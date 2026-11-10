"""On the dashboard the Farm next Buy mark and the price's colour follow a refreshed price
without reloading the page.

After an ok refresh of a Farm next item's price (its own button, or the Market Watch button of
the same item) the page asks the server for that item's answer and redraws only the row's Buy
mark (before the price, its title the reason as text) and the price's colour: green when buying
is recommended, grey otherwise. The price display stays the very same element and keeps the
focus the refresh moved to it. A failed refresh asks nothing; a failed answer, or one without
the fields, leaves the row as it was; an older answer of the same item that arrives late
changes nothing; an item outside Farm next asks nothing.

The dashboard is rendered by the test client on its temporary database, its stylesheets and
scripts pointed at the app's own static files on disk, and run in a headless Chrome or Edge (a
local subprocess loading a local file, every host name resolving to nothing). The page's
requests are answered in the page by a stand-in for `$.ajax` with the JSON the app's own routes
gave in this test. Skipped when no such browser is installed.
"""
import copy
import html
import json
import os
import re
import subprocess

import pytest

from models.db import get_db
from test_dashboard_farm_next import (  # noqa: F401  (fixtures: admin_id, requests_made)
    STALE_AGE, add_item, admin_id, ago, cache, requests_made, set_setting)
from test_collection_card_height_in_browser import BROWSER

PROBE = """<script>
(function () {
  var F = window.FAKE, asked = [];
  function answer(opts, step) {
    setTimeout(function () {
      if (step.status === 200) { if (opts.success) opts.success(step.body); }
      else if (opts.error) opts.error({status: step.status, responseJSON: step.body});
      if (opts.complete) opts.complete();
    }, step.delay || 0);
    return {};
  }
  $.ajax = function (opts) {
    var url = String(opts.url), m;
    if ((m = /\\/dashboard\\/farm-next\\/([0-9]+)$/.exec(url))) {
      asked.push(m[1]);
      var q = F.answers[m[1]];
      return answer(opts, q.length > 1 ? q.shift() : q[0]);
    }
    if ((m = /\\/market\\/price\\/([0-9]+)$/.exec(url))) return answer(opts, F.prices[m[1]]);
    return {};
  };
  function row(id) {
    return $('tr.farm-next-row').filter(function () { return $(this).attr('data-item') === id; });
  }
  function state(id) {
    var $mark = row(id).find('.farm-next-buy'), $p = row(id).find('.farm-next-price > .wf-price');
    return {marks: $mark.length, mark: $mark.length ? $mark.text().trim() : null,
            title: $mark.length ? $mark.attr('title') : null,
            beforePrice: $mark.length ? $mark.next()[0] === $p[0] : null,
            markup: row(id).find('.farm-next-price b, .farm-next-price img').length,
            green: $p.hasClass('green-text'), grey: $p.hasClass('grey-text'),
            value: $p.find('.wf-price-value').text()};
  }
  function button(step) {
    var $in = step.where === 'watch' ? $('#market-watch') : $('.farm-next-row');
    return $in.find('.wf-price-refresh').filter(function () {
      return $(this).attr('data-item') === step.click;
    })[0];
  }
  $(window).on('load', function () {
    var before = {}, prices = {};
    F.rows.forEach(function (id) {
      before[id] = state(id);
      prices[id] = row(id).find('.wf-price')[0];
    });
    var focused = null;
    F.steps.forEach(function (step) {
      setTimeout(function () {
        if (step.click) {
          var b = button(step);
          b.focus();
          b.click();
          focused = step.click;
        }
        if (step.event) $(document).trigger('wf-price-refreshed', [step.event]);
      }, step.at || 0);
    });
    setTimeout(function () {
      var after = {}, kept = {};
      F.rows.forEach(function (id) {
        after[id] = state(id);
        kept[id] = document.contains(prices[id]);
      });
      var active = document.activeElement;
      var out = {before: before, after: after, kept: kept, asked: asked,
                 focusOnPrice: focused ? $(active).is('.wf-price-value') &&
                                         $(active).closest('.wf-price').attr('data-item') === focused
                                       : null};
      var pre = document.createElement('pre');
      pre.id = 'result';
      pre.textContent = JSON.stringify(out);
      document.body.appendChild(pre);
    }, 900);
  });
})();
</script>"""


def run_page(app, page, fake, tmp_path):
    static = os.path.abspath(os.path.join(app.root_path, 'static')).replace(os.sep, '/')
    page = re.sub(r'(href|src)="/static/', lambda m: f'{m.group(1)}="file:///{static}/', page)
    fake_js = '<script>window.FAKE = %s;</script>' % json.dumps(fake).replace('</', '<\\/')
    page = page.replace('</body>', fake_js + PROBE + '</body>', 1)
    target = tmp_path / 'dashboard.html'
    target.write_text(page, encoding='utf-8')
    done = subprocess.run(
        [BROWSER, '--headless=new', '--disable-gpu', '--no-first-run',
         '--host-resolver-rules=MAP * ~NOTFOUND', '--disable-background-networking',
         '--disable-component-update', '--disable-sync', '--no-default-browser-check',
         f'--user-data-dir={tmp_path / "profile"}', '--window-size=1400,1200',
         '--virtual-time-budget=5000', '--dump-dom', target.as_uri()],
        capture_output=True, text=True, encoding='utf-8', timeout=60)
    found = re.search(r'<pre id="result">(.*?)</pre>', done.stdout, re.S)
    assert found, done.stderr[-500:]
    return json.loads(html.unescape(found.group(1)))


def step(response):
    return {'status': response.status_code, 'body': response.get_json()}


@pytest.fixture
def dashboard(app, admin_client, admin_id, requests_made):
    """Akbronco Prime (old 60p, watched too) and Braton Prime (old 40p) in Farm next, Lato
    Prime owned and watched (Market Watch only), a 50p threshold; the page as loaded, then each
    item's refresh answer (Akbronco to 40p: buy; Braton to 70p: farm) and its Farm next answer
    before and after the refresh."""
    with app.app_context():
        akbronco = add_item('Akbronco Prime', 'akbronco_prime_set')
        braton = add_item('Braton Prime', 'braton_prime_set')
        lato = add_item('Lato Prime', 'lato_prime_set')
        db = get_db()
        db.execute('INSERT INTO user_progress (user_id, item_id, in_possession) VALUES (?, ?, 1)',
                   (admin_id, lato))
        for item, price in ((akbronco, 60), (braton, 40), (lato, 30)):
            cache(item, price, ago(STALE_AGE))
        for item in (akbronco, lato):
            db.execute('INSERT INTO user_market_watchlist (user_id, item_id) VALUES (?, ?)',
                       (admin_id, item))
        db.commit()
        set_setting(admin_id, 'buy_threshold_plat', '50')
    ids = {name: str(i) for name, i in
           (('akbronco', akbronco), ('braton', braton), ('lato', lato))}
    data = {'ids': ids, 'page': admin_client.get('/dashboard').get_data(as_text=True)}
    data['before'] = {k: step(admin_client.get(f'/dashboard/farm-next/{i}'))
                      for k, i in ids.items() if k != 'lato'}
    with app.app_context():
        for item, price in ((akbronco, 40), (braton, 70), (lato, 25)):
            get_db().execute('UPDATE market_cache SET platinum_price = ?, fetched_at = ? '
                             'WHERE item_id = ?', (price, ago(STALE_AGE * 0), item))
        get_db().commit()
    data['after'] = {k: step(admin_client.get(f'/dashboard/farm-next/{i}'))
                     for k, i in ids.items() if k != 'lato'}
    fetched = {'fetched_at': '2026-11-10T12:00:00', 'fetched_label': '2026-11-10 12:00 UTC',
               'from_cache': False}
    data['prices'] = {i: {'status': 200, 'body': dict(fetched, ok=True, platinum=p)}
                      for i, p in ((ids['akbronco'], 40), (ids['braton'], 70), (ids['lato'], 25))}
    assert data['before']['akbronco']['body']['recommend_buy'] is False
    assert data['after']['akbronco']['body']['recommend_buy'] is True
    assert data['before']['braton']['body']['recommend_buy'] is True
    assert data['after']['braton']['body']['recommend_buy'] is False
    assert requests_made == []
    return data


def scenario(dash, steps, answers=None, prices=None):
    ids = dash['ids']
    return {'rows': [ids['akbronco'], ids['braton']], 'steps': steps,
            'answers': answers or {ids[k]: [dash['after'][k]] for k in ('akbronco', 'braton')},
            'prices': prices or dash['prices']}


def drawn(answer, value):
    """The row's state as the template draws it from a Farm next answer."""
    buy = answer['body']['recommend_buy']
    return {'marks': 1 if buy else 0, 'mark': 'Buy' if buy else None,
            'title': answer['body']['recommend_reason'] if buy else None,
            'beforePrice': True if buy else None, 'markup': 0,
            'green': buy, 'grey': not buy, 'value': value}


needs_browser = pytest.mark.skipif(BROWSER is None, reason='no headless Chrome or Edge installed')


@needs_browser
@pytest.mark.parametrize('where', ['farm', 'watch'], ids=['Farm next button', 'Market Watch button'])
def test_an_ok_refresh_redraws_only_its_rows_mark_and_colour(app, dashboard, tmp_path, where):
    ids = dashboard['ids']
    result = run_page(app, dashboard['page'],
                      scenario(dashboard, [{'click': ids['akbronco'], 'where': where}]), tmp_path)
    assert result['before'][ids['akbronco']] == drawn(dashboard['before']['akbronco'], '60p')
    assert result['after'][ids['akbronco']] == drawn(dashboard['after']['akbronco'], '40p')
    assert result['after'][ids['braton']] == result['before'][ids['braton']]
    assert result['asked'] == [ids['akbronco']]
    assert result['kept'] == {ids['akbronco']: True, ids['braton']: True}
    assert result['focusOnPrice'] is True


@needs_browser
def test_a_buy_mark_goes_when_the_refreshed_price_is_no_buy(app, dashboard, tmp_path):
    ids = dashboard['ids']
    result = run_page(app, dashboard['page'],
                      scenario(dashboard, [{'click': ids['braton']}]), tmp_path)
    assert result['before'][ids['braton']] == drawn(dashboard['before']['braton'], '40p')
    assert result['after'][ids['braton']] == drawn(dashboard['after']['braton'], '70p')
    assert result['asked'] == [ids['braton']]


@needs_browser
def test_a_failed_refresh_asks_nothing_and_changes_no_row(app, dashboard, tmp_path):
    ids = dashboard['ids']
    prices = dict(dashboard['prices'])
    prices[ids['akbronco']] = {'status': 502, 'body': {'ok': False, 'error': 'down',
                                                       'platinum': 60}}
    result = run_page(app, dashboard['page'],
                      scenario(dashboard, [{'click': ids['akbronco']}], prices=prices), tmp_path)
    assert result['asked'] == []
    assert result['after'] == result['before']


ANSWER_FAILURES = [
    ('500', {'status': 500, 'body': {'ok': False, 'error': 'boom'}}),
    ('404', {'status': 404, 'body': {'ok': False, 'error': 'No market listing'}}),
    ('ok false', {'status': 200, 'body': {'ok': False, 'recommend_buy': True,
                                          'recommend_reason': 'x'}}),
    ('no fields', {'status': 200, 'body': {'ok': True}}),
    ('no reason field', {'status': 200, 'body': {'ok': True, 'recommend_buy': True}}),
    ('buy not a boolean', {'status': 200, 'body': {'ok': True, 'recommend_buy': 'yes',
                                                   'recommend_reason': 'x'}}),
    ('no answer', {'status': 200, 'body': None}),
]


@needs_browser
@pytest.mark.parametrize('failure', [f[1] for f in ANSWER_FAILURES],
                         ids=[f[0] for f in ANSWER_FAILURES])
def test_a_failed_answer_leaves_the_row_as_it_was(app, dashboard, tmp_path, failure):
    ids = dashboard['ids']
    answers = {ids['akbronco']: [failure], ids['braton']: [dashboard['after']['braton']]}
    result = run_page(app, dashboard['page'],
                      scenario(dashboard, [{'click': ids['akbronco']}], answers=answers), tmp_path)
    assert result['asked'] == [ids['akbronco']]
    after = result['after'][ids['akbronco']]
    assert dict(after, value='60p') == result['before'][ids['akbronco']]
    assert after['value'] == '40p'


@needs_browser
def test_a_late_answer_of_an_older_request_changes_nothing(app, dashboard, tmp_path):
    ids = dashboard['ids']
    old = dict(copy.deepcopy(dashboard['before']['akbronco']), delay=300)
    answers = {ids['akbronco']: [old, dashboard['after']['akbronco']],
               ids['braton']: [dashboard['after']['braton']]}
    result = run_page(app, dashboard['page'], scenario(
        dashboard, [{'event': ids['akbronco']}, {'event': ids['akbronco'], 'at': 50}],
        answers=answers), tmp_path)
    assert result['asked'] == [ids['akbronco'], ids['akbronco']]
    assert result['after'][ids['akbronco']] == drawn(dashboard['after']['akbronco'], '60p')


@needs_browser
def test_answers_of_two_items_out_of_order_each_redraw_their_own_row(app, dashboard, tmp_path):
    ids = dashboard['ids']
    answers = {ids['akbronco']: [dict(dashboard['after']['akbronco'], delay=300)],
               ids['braton']: [dashboard['after']['braton']]}
    result = run_page(app, dashboard['page'], scenario(
        dashboard, [{'click': ids['akbronco']}, {'click': ids['braton'], 'at': 20}],
        answers=answers), tmp_path)
    assert sorted(result['asked']) == sorted([ids['akbronco'], ids['braton']])
    assert result['after'][ids['akbronco']] == drawn(dashboard['after']['akbronco'], '40p')
    assert result['after'][ids['braton']] == drawn(dashboard['after']['braton'], '70p')


@needs_browser
def test_an_item_only_in_market_watch_asks_nothing(app, dashboard, tmp_path):
    ids = dashboard['ids']
    result = run_page(app, dashboard['page'], scenario(
        dashboard, [{'click': ids['lato'], 'where': 'watch'}]), tmp_path)
    assert result['asked'] == []
    assert result['after'] == result['before']


@needs_browser
def test_a_reason_with_markup_is_drawn_as_text(app, dashboard, tmp_path):
    ids = dashboard['ids']
    hostile = copy.deepcopy(dashboard['after']['akbronco'])
    hostile['body']['recommend_reason'] = '<b>bold</b><img src="x" onerror="1">'
    answers = {ids['akbronco']: [hostile], ids['braton']: [dashboard['after']['braton']]}
    result = run_page(app, dashboard['page'],
                      scenario(dashboard, [{'click': ids['akbronco']}], answers=answers), tmp_path)
    assert result['after'][ids['akbronco']] == drawn(hostile, '40p')
