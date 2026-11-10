"""Component prices in the Farming Planner: shown in both planner modes from the cache only,
a refresh per click through their own route, and the buy-or-farm answer per component.

Oracles: plan opens with every market request refused (price views for no row, fresh, old and a
NULL price; one query); the answer per component from its own price and its part's time; the
refresh route (fresh: no request; old and answered; old and failed: nothing written, the kept
price; no slug, no item: 404; names with ``/``, ``%``, ``<``); the page script of both modes.
Every request is mocked at ``requests.get``; no test reaches the network.
"""
from urllib.parse import quote

import pytest
import requests

from models.db import get_db
from services import component_prices
from services import market_slugs as ms
from services.farming_service import (add_farming_goal, get_goal_plan_with_progress,
                                      set_goal_progress)
from component_price_fixtures import (  # noqa: F401  (fixtures: market, admin_id, refused)
    OLD_AT, FakeResponse, add_item, admin_id, ash_goal, cache_slug, components_of, drop,
    item_list, listed, market, meta, now_text, refused, set_component_slug, slug_cache,
    store_setting, stored_component_slugs)


# ── Opening a plan reads the cache only (axes 4, 9) ──────────────────────────

# (component, platinum, cached, stale, refresh)
VIEW_ROWS = [
    ('Neuroptics', None, False, False, True),
    ('Chassis', 40, True, False, False),
    ('Systems', 15, True, True, True),
    ('Blueprint', None, True, False, False),
]


@pytest.mark.parametrize('row', VIEW_ROWS, ids=lambda r: r[0])
def test_opening_a_plan_shows_each_component_price_from_the_cache_only(app, admin_client,
                                                                     admin_id, refused, row):
    component, platinum, cached, stale, refresh = row
    with app.app_context():
        _, goal = ash_goal(admin_id)
    for _ in range(2):                                   # opened twice: nothing requested
        response = admin_client.get(f'/farming/goals/{goal}/plan')
        assert response.status_code == 200
        comp = components_of(response.get_json()['plan'])[component]
        view = comp['market']
        assert (view['platinum'], view['cached'], view['stale'], view['refresh']) == \
            (platinum, cached, stale, refresh)
        assert view['market_url'] == f'https://warframe.market/items/{comp["market_slug"]}'


def test_a_component_without_a_slug_has_no_price_and_no_answer(app, admin_id, refused):
    with app.app_context():
        _, goal = ash_goal(admin_id)
        helmet = components_of(get_goal_plan_with_progress(goal, admin_id))['Helmet']
    assert (helmet['market'], helmet['market_slug'], helmet['recommend_buy'],
            helmet['recommend_reason'], helmet['expected_hours']) == (None, None, False, None, None)


def test_the_plan_reads_the_component_prices_in_one_query(app, admin_id, refused):
    with app.app_context():
        _, goal = ash_goal(admin_id)
        statements = []
        get_db().set_trace_callback(statements.append)
        get_goal_plan_with_progress(goal, admin_id)
        get_db().set_trace_callback(None)
    reads = [s for s in statements if 'market_slug_cache' in s]
    assert len(reads) == 1 and reads[0].lstrip().startswith('SELECT')


def test_a_slug_spelled_otherwise_than_the_plans_part_still_matches(app, admin_id, refused):
    with app.app_context():
        ash = add_item('Ash Prime')
        drop(ash, 'Neuroptics')
        set_component_slug(ash, ' NEUROPTICS ', 'ash_n')
        cache_slug('ash_n', 12, now_text())
        goal = add_farming_goal(admin_id, ash)['goal_id']
        comp = components_of(get_goal_plan_with_progress(goal, admin_id))['Neuroptics']
    assert comp['market']['platinum'] == 12


# ── The answer per component ─────────────────────────────────────────────────

# (case id, settings, component, recommend, reason)
ANSWER_ROWS = [
    ('no setting: farm, with the part time', {}, 'Chassis', False, '~0.5h farming'),
    ('the threshold on the part price', {'buy_threshold_plat': '15'}, 'Systems', True,
     '15p ≤ threshold 15p'),
    ('the threshold not reached', {'buy_threshold_plat': '15'}, 'Chassis', False,
     '~0.5h farming'),
    ('the time value with the part time', {'plat_per_hour': '100'}, 'Chassis', True,
     '~0.5h farming × 100p/h > market 40p'),
    ('the hour cap without a price', {'farm_hour_cap': '0'}, 'Neuroptics', False,
     '~0.5h farming'),
]


@pytest.mark.parametrize('row', ANSWER_ROWS, ids=lambda r: r[0])
def test_each_component_gets_the_buy_or_farm_answer_of_its_own_price_and_time(
        app, admin_id, refused, row):
    _, settings, component, buy, reason = row
    with app.app_context():
        _, goal = ash_goal(admin_id)
        for key, value in settings.items():
            store_setting(admin_id, key, value)
        comp = components_of(get_goal_plan_with_progress(goal, admin_id))[component]
    assert (comp['recommend_buy'], comp['recommend_reason']) == (buy, reason)
    assert comp['expected_hours'] == pytest.approx(0.5)


def test_a_component_only_vaulted_relics_hold_recommends_buying(app, admin_id, refused):
    with app.app_context():
        ash = add_item('Ash Prime')
        get_db().execute(
            '''INSERT INTO drop_sources (item_id, mission_type, location, drop_chance, avg_runs,
                                         source_type, notes)
               VALUES (?, 'Relic', 'Lith Z9 Relic', 10, NULL, 'relic', 'Neuroptics')''', (ash,))
        get_db().execute("INSERT INTO relic_drops (relic_name, location) "
                         "VALUES ('Lith A1 Relic', 'Earth/Mantle')")
        get_db().commit()
        set_component_slug(ash, 'Neuroptics', 'ash_n')
        goal = add_farming_goal(admin_id, ash)['goal_id']
        comp = components_of(get_goal_plan_with_progress(goal, admin_id))['Neuroptics']
    assert (comp['recommend_buy'], comp['recommend_reason'], comp['expected_hours']) == \
        (True, 'Neuroptics vaulted: no mission drops it', None)


def test_a_crafted_part_keeps_its_price_and_answer(app, admin_id, refused):
    with app.app_context():
        _, goal = ash_goal(admin_id)
        set_goal_progress(admin_id, goal, 'component', 'Chassis', comp_state='crafted')
        comp = components_of(get_goal_plan_with_progress(goal, admin_id))['Chassis']
    assert comp['market']['platinum'] == 40 and comp['recommend_reason'] == '~0.5h farming'


# ── The refresh route (axes 1, 4, 6, 7) ──────────────────────────────────────

def refresh_url(item_id, component):
    return f'/market/component-price/{item_id}?component={quote(component, safe="")}'


def test_a_fresh_price_answers_from_the_cache_without_a_request(app, admin_client, admin_id,
                                                                market):
    with app.app_context():
        ash, _ = ash_goal(admin_id)
        before = slug_cache()
    answer = admin_client.get(refresh_url(ash, 'Chassis'))
    assert answer.status_code == 200
    body = answer.get_json()
    assert (body['ok'], body['platinum'], body['from_cache']) == (True, 40, True)
    assert market.calls == []
    with app.app_context():
        assert slug_cache() == before


@pytest.mark.parametrize('component, slug', [('Systems', 'ash_s'), ('Neuroptics', 'ash_n')],
                         ids=['old row', 'no row'])
def test_an_old_or_missing_price_is_asked_once_and_written(app, admin_client, admin_id,
                                                           market, component, slug):
    with app.app_context():
        ash, _ = ash_goal(admin_id)
    body = admin_client.get(refresh_url(ash, component)).get_json()
    assert (body['ok'], body['platinum'], body['from_cache']) == (True, 22, False)
    assert market.calls == [f'https://api.warframe.market/v2/orders/item/{slug}']
    with app.app_context():
        assert slug_cache()[slug] == (22, body['fetched_at'])


MARKET_FAILURES = [
    ('market down', requests.ConnectionError('refused')),
    ('timeout', requests.Timeout('slow')),
    ('too many requests', FakeResponse(status=429)),
    ('server error', FakeResponse(status=503)),
    ('not json', FakeResponse(payload='<html>')),
    ('not v2 shape', FakeResponse(payload={'payload': {'orders': []}})),
    ('an error reported', FakeResponse(payload={'data': [], 'error': 'not found'})),
]


@pytest.mark.parametrize('case, failure', MARKET_FAILURES, ids=[c[0] for c in MARKET_FAILURES])
def test_a_failed_refresh_writes_nothing_and_answers_with_the_kept_price(
        app, admin_client, admin_id, market, case, failure):
    with app.app_context():
        ash, _ = ash_goal(admin_id)
        before = slug_cache()
    market.orders = failure
    answer = admin_client.get(refresh_url(ash, 'Systems'))
    assert answer.status_code == 502
    body = answer.get_json()
    assert (body['ok'], body['platinum'], body['fetched_at']) == (False, 15, OLD_AT)
    assert body['fetched_label'] == '2026-01-01 10:00 UTC' and 'kept' in body['error']
    with app.app_context():
        assert slug_cache() == before


def test_a_failed_refresh_without_a_cached_price_answers_none(app, admin_client, admin_id,
                                                              market):
    with app.app_context():
        ash, _ = ash_goal(admin_id)
    market.orders = requests.Timeout('slow')
    body = admin_client.get(refresh_url(ash, 'Neuroptics')).get_json()
    assert (body['ok'], body['platinum'], body['fetched_at']) == (False, None, None)
    with app.app_context():
        assert 'ash_n' not in slug_cache()


@pytest.mark.parametrize('path', [
    'component-price/{ash}?component=Helmet',            # a part without a slug
    'component-price/{ash}?component=Nothing',           # no such part
    'component-price/{ash}?component=',                  # empty
    'component-price/{ash}?component=%20%20',            # blanks only
    'component-price/{ash}',                             # no component value
    'component-price/{ash}?component=' + 'x' * 501,      # longer than the column
    'component-price/987654?component=Chassis',          # no such item
], ids=['no slug', 'no such part', 'empty', 'blanks', 'missing', 'too long', 'no item'])
def test_a_component_without_a_listing_answers_404_without_a_request(app, admin_client, admin_id,
                                                                     market, path):
    with app.app_context():
        ash, _ = ash_goal(admin_id)
    answer = admin_client.get('/market/' + path.format(ash=ash))
    assert answer.status_code == 404 and answer.get_json()['ok'] is False
    assert market.calls == []


@pytest.mark.parametrize('name', ['Barrel/Stock', '100% Chassis', '<b>Neuro</b> "x"',
                                  'Systems?x=1', 'Wings & Harness', 'Ōmen Part'])
def test_a_component_name_with_route_characters_reaches_its_own_slug(app, admin_client,
                                                                     admin_id, market, name):
    with app.app_context():
        ash = add_item('Ash Prime')
        set_component_slug(ash, name, 'odd_slug')
        cache_slug('odd_slug', 7, now_text())
    answer = admin_client.get(refresh_url(ash, name))
    assert answer.status_code == 200 and answer.get_json()['platinum'] == 7
    assert market.calls == []


def test_a_sync_that_drops_the_slug_while_a_plan_is_open_makes_the_refresh_404(
        app, admin_client, admin_id, market):
    with app.app_context():
        ash, goal = ash_goal(admin_id)
    assert admin_client.get(f'/farming/goals/{goal}/plan').status_code == 200
    market.items = FakeResponse(payload=item_list([listed('Ash Prime Set', 'ash_set')]))
    with app.app_context():
        assert ms.sync_market_slugs()['components_matched'] == 0
    assert admin_client.get(refresh_url(ash, 'Systems')).status_code == 404
    assert all('/orders/item/' not in url for url in market.calls)


def test_the_refresh_route_needs_a_login(app, client):
    assert client.get('/market/component-price/1?component=Chassis').status_code in (302, 401)


def test_a_price_written_by_another_request_in_between_is_updated_not_duplicated(app, market):
    """Two refreshes of one component: the second insert meets the first one's row."""
    with app.app_context():
        db = get_db()

        class Racing:
            """The update finds no row; another request inserts it before this insert."""
            def __init__(self):
                self.raced = False

            def execute(self, sql, params=()):
                if sql.startswith('INSERT') and not self.raced:
                    self.raced = True
                    db.execute('INSERT INTO market_slug_cache (market_url_name, platinum_price, '
                               "fetched_at) VALUES ('ash_c', 99, ?)", (OLD_AT,))
                    db.commit()
                return db.execute(sql, params)

            def commit(self):
                db.commit()

            def rollback(self):
                db.rollback()

        component_prices._store_slug_price(Racing(), 'ash_c', 22, '2026-11-10T08:00:00')
        assert slug_cache() == {'ash_c': (22, '2026-11-10T08:00:00')}


# ── The page (axis 9: both modes, text only) ─────────────────────────────────

PAGE_HOOKS = [
    # tree view: on the component row
    "html += compPriceMarkup(plan, comp, 'font-size:.78rem');",
    # visual plan: under the component circle, by the plan's component of that name
    "html += compPriceMarkup(plan, planComps[comp.component_name],",
    "var planComps = Object.create(null);",
    # every value is drawn as text
    "escText(wfPriceText(p.platinum))",
    "' data-component=\"' + escText(comp.component_name) + '\">'",
    "escText(plan.item_name + ' ' + comp.component_name)",
    "'old (' + escText(p.fetched_label) + ')</span>'",
    "escText(p.market_url)",
    "buyVsFarmNote(comp, true)",
    # the slug and the item id are checked before they become attributes
    "var WF_COMP_SLUG = /^[A-Za-z0-9_-]{1,255}$/;",
    "if (!p || !WF_COMP_SLUG.test(slug) || !WF_COMP_ITEM.test(itemId)) return '';",
    # one route, the name as a query value; a click while the slug's request runs is ignored
    "data: {component: name},",
    "wfCompPriceBusy[slug]) return;",
    "delete wfCompPriceBusy[slug];",
    # only ok true is a success; a failure keeps the price, marked old
    "if (r && r.ok === true) wfPriceShowIn($prices, r, true, null);",
    "error: function (xhr) { failed(xhr.responseJSON, 'the request failed.'); },",
    # the toast's markup is the escaped error
    "M.toast({html: 'The price was not refreshed: ' + escText(error)});",
    # the attributes are escaped, and the displays of a slug are found by comparison, not a
    # selector built of the slug
    "data-slug=\"' + escText(slug) + '\"'",
    "' data-item=\"' + escText(itemId) + '\"'",
    "return $(this).attr('data-slug') === slug;",
]


@pytest.mark.parametrize('hook', PAGE_HOOKS)
def test_both_planner_modes_draw_the_component_price_as_text(admin_client, hook):
    page = admin_client.get('/farming/').get_data(as_text=True)
    assert hook in page


def test_the_page_carries_the_route_of_the_component_refresh(admin_client):
    page = admin_client.get('/farming/').get_data(as_text=True)
    assert 'var COMP_PRICE_URL = "/market/component-price/0";' in page
