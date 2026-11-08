"""Prime Resurgence availability where the user sees it: the collection's filter, badges and
highlights (card grid and table), the dashboard's alerts and the planner's vaulted note. Each
shows the stock only while it is current; otherwise each shows the notice instead.
"""
import pytest

from models.db import get_db
from models.progress import Progress
from services import resurgence_sync as rs
from services.auth_service import set_global_setting

FUTURE = '2999-01-01T18:00:00Z'
PAST = '2000-01-01T18:00:00Z'
STATES = {'current': FUTURE, 'expired': PAST, 'missing': None}
NOTICES = {'current': None, 'expired': rs.NOTICE_EXPIRED, 'missing': rs.NOTICE_MISSING}


@pytest.fixture
def world(app, admin_client):
    """Nidus Prime (vaulted, in the stock, watched), Saryn Prime (vaulted, not in the stock)
    and Braton (not vaulted); returns a function that sets the stock's freshness."""
    with app.app_context():
        db = get_db()
        for name, vaulted, avail in (('Nidus Prime', 1, 1), ('Saryn Prime', 1, 0),
                                     ('Braton', 0, 0)):
            db.execute('INSERT INTO items (name, category, source, vaulted, resurgence_available)'
                       ' VALUES (?, ?, ?, ?, ?)',
                       (name, 'warframe', 'prime' if 'Prime' in name else 'standard',
                        vaulted, avail))
        ids = {r['name']: r['id'] for r in db.execute('SELECT id, name FROM items')}
        for name in ids:
            db.execute("INSERT INTO drop_sources (item_id, mission_type, location, drop_chance,"
                       " avg_runs, source_type, notes) VALUES (?, 'Relic', 'Lith N1', 10, 10,"
                       " 'component', ?)", (ids[name], name + ' Chassis Blueprint'))
        db.commit()
        Progress.upsert(1, ids['Nidus Prime'], resurgence_watch=1)

    def set_state(state):
        with app.app_context():
            if STATES[state] is not None:
                set_global_setting(rs.SETTING_ENDS_AT, STATES[state])
        return ids

    return set_state


@pytest.mark.parametrize('state', STATES)
def test_the_available_filter_lists_the_stock_only_while_it_is_current(admin_client, world, state):
    world(state)
    body = admin_client.get('/collection/warframe?resurgence=available').get_data(as_text=True)
    assert ('title="Nidus Prime"' in body) is (state == 'current')
    assert 'title="Saryn Prime"' not in body
    assert ('wf-resurgence-notice' in body) is (state != 'current')
    if state != 'current':
        assert NOTICES[state] in body


@pytest.mark.parametrize('state', STATES)
def test_badges_and_highlights_show_only_while_the_stock_is_current(admin_client, world, state):
    world(state)
    body = admin_client.get('/collection/warframe').get_data(as_text=True)
    current = state == 'current'
    assert ('Resurgence!' in body) is current                       # card badge
    assert ('resurgence-card' in body) is current                   # card grid highlight
    assert ('resurgence-row' in body) is current                    # table view highlight
    assert ('data-f-ravail="1"' in body) is current                 # client-side filter value
    if not current:
        assert NOTICES[state] in body


def test_mods_and_arcanes_show_no_resurgence_notice(admin_client, world):
    world('missing')
    for cat in ('mod', 'arcane'):
        assert 'wf-resurgence-notice' not in admin_client.get(f'/collection/{cat}').get_data(
            as_text=True)


@pytest.mark.parametrize('state', STATES)
def test_the_dashboard_alerts_only_while_the_stock_is_current(admin_client, world, state):
    world(state)
    body = admin_client.get('/dashboard').get_data(as_text=True)
    current = state == 'current'
    assert ('Available Now!' in body) is current
    assert ('<strong>Nidus Prime</strong>' in body) is current
    assert ('wf-resurgence-notice' in body) is (not current)
    if not current:
        assert NOTICES[state] in body


def _plan(client, item_id):
    goal = client.post('/farming/goals/add', json={'item_id': item_id}).get_json()['goal']
    return client.get(f'/farming/goals/{goal["goal_id"]}/plan').get_json()['plan']


@pytest.mark.parametrize('state', STATES)
@pytest.mark.parametrize('item, vaulted, in_stock', [
    ('Nidus Prime', True, True), ('Saryn Prime', True, False), ('Braton', False, False)])
def test_the_planner_names_trade_and_prime_resurgence_for_a_vaulted_item(
        admin_client, world, state, item, vaulted, in_stock):
    ids = world(state)
    plan = _plan(admin_client, ids[item])
    now = vaulted and in_stock and state == 'current'
    expected_note = (rs.vaulted_note(now) if vaulted else None)
    assert plan['vaulted'] is vaulted
    assert plan['vaulted_note'] == expected_note
    assert plan['in_resurgence'] is now
    assert plan['resurgence_notice'] == (NOTICES[state] if vaulted else None)
    assert [c['vaulted_note'] for c in plan['components']] == [expected_note]
    if vaulted:
        assert 'trade' in expected_note and 'Prime Resurgence' in expected_note
        assert ('in Prime Resurgence now' in expected_note) is now


def test_the_planner_page_renders_the_note_in_both_views(admin_client, world):
    world('current')
    body = admin_client.get('/farming/').get_data(as_text=True)
    # both renderers start with the vaulted note and mark each vaulted component
    assert body.count('vaultedPlanNote(plan)') == 3       # the definition's use + two views
    assert 'vaultedLock(comp.vaulted_note, plan.in_resurgence)' in body       # tree view
    assert 'vaultedLock(plan.vaulted_note, false)' in body                    # visual plan
