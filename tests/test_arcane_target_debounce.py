"""The arcane target rank input waits 600 ms after the last edit before it saves, per item.

Each item has its own debounce: an edit of one card cancels only a pending save of the same
item (its card or its table row), never another card's. So two cards edited within the window
are both saved, each with its own last value; the same card edited twice saves once, with the
last value; one edit that fires both `input` and `change` saves once.

Checked in a headless Chrome or Edge with the page's AJAX answered locally and each save
recorded (skipped without one).
"""
import json

import pytest

from test_arcane_rank_bar import add_arcane
from test_collection_card_height_in_browser import BROWSER, head_heights  # noqa: F401
from test_collection_prices import page

PROBE = """<script>
window.addEventListener('load', function () { setTimeout(function () {
  // jQuery runs the page's ready handlers after load's turn: bind first, then act.
  var saves = [];
  $.ajax = function (o) {
    var d = JSON.parse(o.data);
    saves.push(d.item_id + ':' + d.target_rank);
    o.success({ok: true, target_rank: d.target_rank});
  };
  var steps = __STEPS__;   // [at ms, view, item id, value, [events]]
  steps.forEach(function (s) {
    setTimeout(function () {
      var $inp = $('#collection-' + s[1] + ' .arc-target-input[data-item="' + s[2] + '"]');
      $inp.val(s[3]);
      s[4].forEach(function (ev) { $inp.trigger(ev); });
    }, s[0]);
  });
  var last = Math.max.apply(null, steps.map(function (s) { return s[0]; }));
  setTimeout(function () {   // well past the last edit's 600 ms
    var pre = document.createElement('pre');
    pre.id = 'heads';
    pre.textContent = saves.map(function (s, i) { return 'save' + i + '=' + s; }).join('\\n');
    document.body.appendChild(pre);
  }, last + 1000);
}, 200); });
</script>"""

# name: ([(at ms, view, card 'A' or 'B', value, events)], the saves in order as (card, value))
MATRIX = {
    'two cards within the window': (
        [(0, 'cards', 'A', 3, ['change']), (200, 'cards', 'B', 4, ['change'])],
        [('A', 3), ('B', 4)]),
    'two cards interleaved within the window': (
        [(0, 'cards', 'A', 1, ['input']), (100, 'cards', 'B', 2, ['input']),
         (200, 'cards', 'A', 3, ['input']), (300, 'cards', 'B', 4, ['input'])],
        [('A', 3), ('B', 4)]),
    'same card twice within the window': (
        [(0, 'cards', 'A', 2, ['change']), (300, 'cards', 'A', 5, ['change'])],
        [('A', 5)]),
    'two cards more than the window apart': (
        [(0, 'cards', 'A', 3, ['change']), (800, 'cards', 'B', 4, ['change'])],
        [('A', 3), ('B', 4)]),
    'one edit firing input then change': (
        [(0, 'cards', 'A', 4, ['input', 'change'])],
        [('A', 4)]),
    'card then table of the same item within the window': (
        [(0, 'cards', 'A', 2, ['change']), (200, 'table', 'A', 5, ['change'])],
        [('A', 5)]),
    'three cards within the window': (
        [(0, 'cards', 'A', 1, ['change']), (100, 'cards', 'B', 2, ['change']),
         (200, 'cards', 'C', 3, ['change'])],
        [('A', 1), ('B', 2), ('C', 3)]),
    'card and table views of two items interleaved': (
        [(0, 'cards', 'A', 1, ['input']), (100, 'table', 'B', 2, ['input']),
         (200, 'table', 'A', 3, ['change']), (300, 'cards', 'B', 4, ['change'])],
        [('A', 3), ('B', 4)]),
    'same card twice, the window apart': (
        [(0, 'cards', 'A', 2, ['change']), (800, 'cards', 'A', 5, ['change'])],
        [('A', 2), ('A', 5)]),
}


@pytest.mark.skipif(BROWSER is None, reason='no headless Chrome or Edge installed')
@pytest.mark.parametrize('name', list(MATRIX))
def test_each_item_saves_its_last_target_after_its_own_debounce(
        app, admin_client, tmp_path, monkeypatch, name):
    import test_collection_card_height_in_browser as browser
    steps, saves = MATRIX[name]
    with app.app_context():
        ids = {'A': add_arcane('Arcane Alpha', 0, 0, 5), 'B': add_arcane('Arcane Beta', 0, 0, 5),
               'C': add_arcane('Arcane Gamma', 0, 0, 5)}
    script = [[at, view, ids[card], value, events] for at, view, card, value, events in steps]
    monkeypatch.setattr(browser, 'PROBE', PROBE.replace('__STEPS__', json.dumps(script)))
    shown = head_heights(app, page(admin_client, 'arcane'), tmp_path, 1366)
    assert shown == {f'save{i}': f'{ids[card]}:{value}' for i, (card, value) in enumerate(saves)}
