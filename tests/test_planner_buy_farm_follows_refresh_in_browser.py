"""In the Farming Planner the buy-or-farm answers follow a refreshed price without reopening
the plan.

After an ok refresh of the goal item's price or of a component's, each open goal panel showing
that price reads its plan again and replaces only its buy-or-farm notes (the goal's and each
component's) from the new plan; the price display and the rest of the panel stay the very same
elements. A failed refresh reads no plan; a failed re-read, or one without the answer's fields,
leaves the notes as they were; a late re-read answer (another re-read or a redraw of the panel
since) changes nothing.

The planner page is rendered by the test client on its temporary database, its stylesheets and
scripts pointed at the app's own static files on disk, and run in a headless Chrome or Edge (a
local subprocess loading a local file, every host name resolving to nothing). The page's
requests are answered in the page by a stand-in for `$.ajax` with the JSON the app's own routes
gave in this test (the market mocked at ``requests.get``). Skipped when no such browser is
installed.
"""
import copy
import html
import json
import os
import re
import subprocess

import pytest
import requests

from component_price_fixtures import (  # noqa: F401  (fixtures: market, admin_id)
    OLD_AT, FakeResponse, admin_id, ash_goal, market, orders, store_setting)
from models.db import get_db
from test_collection_card_height_in_browser import BROWSER

PROBE = """<script>
(function () {
  var F = window.FAKE, reads = 0, urls = [];
  function answer(opts, step) {
    setTimeout(function () {
      if (step.status === 200) { if (opts.success) opts.success(step.body); }
      else if (opts.error) opts.error({status: step.status, responseJSON: step.body});
      if (opts.complete) opts.complete();
    }, step.delay || 0);
    return {};
  }
  $.ajax = function (opts) {
    var url = String(opts.url);
    if (/\\/farming\\/goals\\/[0-9]+\\/plan(\\?prices=cached)?$/.test(url)) {
      reads++;
      urls.push(url.replace(/^.*\\/plan/, 'plan'));
      return answer(opts, F.plans.length > 1 ? F.plans.shift() : F.plans[0]);
    }
    if (url.indexOf('/market/component-price/') >= 0) return answer(opts, F.comp);
    if (url.indexOf('/market/price/') >= 0) return answer(opts, F.price);
    return {};
  };
  function notes($panel) {
    var goal = $panel.find('.wf-plan-market-row .wf-plan-buy-farm'), comps = {};
    $panel.find('.wf-comp-price').each(function () {
      var n = $(this).parent().find('.wf-plan-buy-farm');
      comps[$(this).attr('data-component')] = n.length === 1 ? n.text().trim() : (n.length ? 'MANY' : null);
    });
    return {goal: goal.length === 1 ? goal.text().trim() : (goal.length ? 'MANY' : null), comps: comps};
  }
  function button($panel, which) {
    if (which === 'goal') return $panel.find('.wf-plan-market-row .wf-price-refresh')[0];
    return $panel.find('.wf-comp-price').filter(function () {
      return $(this).attr('data-component') === which;
    }).find('.wf-comp-price-refresh')[0];
  }
  $(window).on('load', function () {
    var $panel = $('#plan-panel-' + F.goal), $inner = $panel.closest('.wf-goal-expanded-inner');
    $inner.show();
    loadGoalPlan(F.goal, F.mode);
    setTimeout(function () {
      var before = notes($panel), first = $panel.children()[0];
      var price = $(button($panel, F.click[0])).closest('.wf-price, .wf-comp-price')[0];
      var cb = $panel.find('.goal-comp-cb')[0];
      if (cb) cb.checked = !cb.checked;
      var cbChecked = cb ? cb.checked : null;
      if (F.collapse) $inner.hide();
      F.click.forEach(function (which) { button($panel, which).click(); });
      if (F.redraw) setTimeout(function () { loadGoalPlan(F.goal, F.mode); }, 50);
      setTimeout(function () {
        var out = {before: before, after: notes($panel), reads: reads, urls: urls,
                   priceKept: document.contains(price), panelKept: document.contains(first),
                   checkboxKept: cb ? document.contains(cb) && cb.checked === cbChecked : null};
        var pre = document.createElement('pre');
        pre.id = 'result';
        pre.textContent = JSON.stringify(out);
        document.body.appendChild(pre);
      }, 800);
    }, 100);
  });
})();
</script>"""


def run_page(app, page, fake, tmp_path):
    static = os.path.abspath(os.path.join(app.root_path, 'static')).replace(os.sep, '/')
    page = re.sub(r'(href|src)="/static/', lambda m: f'{m.group(1)}="file:///{static}/', page)
    fake_js = '<script>window.FAKE = %s;</script>' % json.dumps(fake).replace('</', '<\\/')
    page = page.replace('</body>', fake_js + PROBE + '</body>', 1)
    target = tmp_path / 'planner.html'
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


def note(answer):
    """The note's text as the planner draws it from an answer, or None for no note."""
    if not answer.get('recommend_reason'):
        return None
    if answer['recommend_buy']:
        return 'Buy: ' + answer['recommend_reason']
    return ('' if answer['expected_hours'] is None else 'Farm: ') + answer['recommend_reason']


def notes_of(plan, shown):
    comps = {c['component_name']: c for c in plan['components']}
    return {'goal': note(plan), 'comps': {name: note(comps[name]) for name in shown}}


@pytest.fixture
def planner(app, admin_client, admin_id, market):
    """Ash Prime (old price 95p, Neuroptics without a price, a 50p threshold): the plan before
    the refreshes (the market down while it opens), the failed and the ok refresh answers of
    the goal price and of Neuroptics, the planner's re-read (`?prices=cached`) after the goal
    price's refresh alone, after Neuroptics' alone (the goal price still old) and after both,
    and the planner page."""
    with app.app_context():
        ash, goal = ash_goal(admin_id, item_slug='ash_prime')
        get_db().execute('INSERT INTO market_cache (item_id, platinum_price, fetched_at) '
                         'VALUES (?, 95, ?)', (ash, OLD_AT))
        get_db().commit()
        store_setting(admin_id, 'buy_threshold_plat', '50')

    def step(response):
        return {'status': response.status_code, 'body': response.get_json()}

    plan_url = f'/farming/goals/{goal}/plan'
    comp_url = f'/market/component-price/{ash}?component=Neuroptics'
    market.orders = requests.ConnectionError('refused')
    data = {'goal': goal, 'before': step(admin_client.get(plan_url)),
            'price_failed': step(admin_client.get(f'/market/price/{ash}')),
            'comp_failed': step(admin_client.get(comp_url))}
    market.orders = FakeResponse(payload=orders(30, 22))
    calls = len(market.calls)
    data['price'] = step(admin_client.get(f'/market/price/{ash}'))
    data['after_goal'] = step(admin_client.get(plan_url + '?prices=cached'))
    data['comp'] = step(admin_client.get(comp_url))
    data['after'] = step(admin_client.get(plan_url + '?prices=cached'))
    with app.app_context():
        get_db().execute('UPDATE market_cache SET platinum_price = 95, fetched_at = ? '
                         'WHERE item_id = ?', (OLD_AT, ash))
        get_db().commit()
    data['after_comp'] = step(admin_client.get(plan_url + '?prices=cached'))
    assert len(market.calls) == calls + 2                 # the two refreshes, no re-read
    assert (data['price_failed']['body']['ok'], data['comp_failed']['body']['ok']) == (False, False)
    assert (data['price']['body']['ok'], data['comp']['body']['ok']) == (True, True)
    data['page'] = admin_client.get('/farming/').get_data(as_text=True)
    return data


def with_mode(answer, mode):
    answer = copy.deepcopy(answer)
    answer['body']['plan']['display_mode'] = mode
    return answer


def late(answer, delay):
    """A plan answer that comes back after `delay` ms with every reason changed."""
    answer = copy.deepcopy(answer)
    plan = answer['body']['plan']
    for part in [plan] + plan['components']:
        if part.get('recommend_reason'):
            part['recommend_reason'] = 'a late answer'
    answer['delay'] = delay
    return answer


def scenario(planner, mode, click, plans=None, failed=False, **flags):
    fake = {'goal': planner['goal'], 'mode': mode, 'click': click,
            'plans': plans or [with_mode(planner['before'], mode), with_mode(planner['after'], mode)],
            'price': planner['price_failed' if failed else 'price'],
            'comp': planner['comp_failed' if failed else 'comp']}
    fake.update(flags)
    return fake


def assert_notes(result, plan):
    shown = sorted(result['before']['comps'])
    assert 'Neuroptics' in shown
    assert result['after'] == notes_of(plan, shown)


needs_browser = pytest.mark.skipif(BROWSER is None, reason='no headless Chrome or Edge installed')
MODES = pytest.mark.parametrize('mode', ['tree', 'list'], ids=['tree view', 'visual plan'])


@needs_browser
@MODES
@pytest.mark.parametrize('click', [['goal'], ['Neuroptics']], ids=['goal price', 'component price'])
def test_an_ok_refresh_redraws_the_buy_or_farm_notes_from_the_new_plan_in_place(
        app, planner, tmp_path, mode, click):
    reread = planner['after_goal' if click == ['goal'] else 'after_comp']
    plans = [with_mode(planner['before'], mode), with_mode(reread, mode)]
    result = run_page(app, planner['page'], scenario(planner, mode, click, plans=plans),
                      tmp_path)
    before, after = planner['before']['body']['plan'], reread['body']['plan']
    assert result['before'] == notes_of(before, sorted(result['before']['comps']))
    assert_notes(result, after)
    changed = {'goal': result['after']['goal'] != result['before']['goal'],
               'Neuroptics': result['after']['comps']['Neuroptics'] !=
               result['before']['comps']['Neuroptics']}
    # only the refreshed price's answer changes: a component refresh keeps the goal's note,
    # which follows the old goal price still shown beside it
    assert changed == ({'goal': True, 'Neuroptics': False} if click == ['goal']
                       else {'goal': False, 'Neuroptics': True})
    assert (result['reads'], result['urls']) == (2, ['plan', 'plan?prices=cached'])
    assert (result['priceKept'], result['panelKept']) == (True, True)
    assert result['checkboxKept'] in ((True,) if mode == 'tree' else (True, None))


@needs_browser
@MODES
@pytest.mark.parametrize('click', [['goal'], ['Neuroptics']], ids=['goal price', 'component price'])
def test_a_failed_refresh_reads_no_plan_and_changes_no_note(app, planner, tmp_path, mode, click):
    result = run_page(app, planner['page'], scenario(planner, mode, click, failed=True), tmp_path)
    assert (result['reads'], result['after']) == (1, result['before'])


REREAD_FAILURES = [
    ('500', {'status': 500, 'body': {'ok': False, 'error': 'boom'}}),
    ('ok false', {'status': 200, 'body': {'ok': False, 'error': 'Goal not found'}}),
    ('no answer fields', {'status': 200, 'body': {'ok': True, 'plan': {'components': [
        {'component_name': 'Neuroptics'}]}}}),
    ('plan null', {'status': 200, 'body': {'ok': True, 'plan': None}}),
]


@needs_browser
@pytest.mark.parametrize('reread', [r[1] for r in REREAD_FAILURES],
                         ids=[r[0] for r in REREAD_FAILURES])
def test_a_failed_plan_re_read_leaves_the_notes_as_they_were(app, planner, tmp_path, reread):
    plans = [with_mode(planner['before'], 'tree'), reread]
    result = run_page(app, planner['page'], scenario(planner, 'tree', ['goal', 'Neuroptics'],
                                                     plans=plans), tmp_path)
    assert (result['reads'], result['after']) == (3, result['before'])


@needs_browser
def test_a_collapsed_goal_reads_no_plan(app, planner, tmp_path):
    result = run_page(app, planner['page'], scenario(planner, 'tree', ['goal'], collapse=True),
                      tmp_path)
    assert (result['reads'], result['after']) == (1, result['before'])


@needs_browser
@MODES
def test_a_late_re_read_answer_after_a_newer_one_changes_nothing(app, planner, tmp_path, mode):
    after = with_mode(planner['after'], mode)
    plans = [with_mode(planner['before'], mode), late(after, 300), after]
    result = run_page(app, planner['page'],
                      scenario(planner, mode, ['goal', 'Neuroptics'], plans=plans), tmp_path)
    assert result['reads'] == 3
    assert_notes(result, planner['after']['body']['plan'])


@needs_browser
@MODES
def test_a_re_read_answer_after_the_panel_was_drawn_again_changes_nothing(app, planner, tmp_path,
                                                                         mode):
    after = with_mode(planner['after'], mode)
    plans = [with_mode(planner['before'], mode), late(after, 300), after]
    result = run_page(app, planner['page'],
                      scenario(planner, mode, ['goal'], plans=plans, redraw=True), tmp_path)
    assert result['reads'] == 3
    assert_notes(result, planner['after']['body']['plan'])
