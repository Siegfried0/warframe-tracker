import json as _json

from flask import Blueprint, jsonify, render_template, request, session
from flask_login import current_user, login_required

from models.progress import Progress
from services.auth_service import get_setting
from services.farming_service import (
    GOAL_MOVE_DIRECTIONS,
    add_farming_goal,
    clear_pinned_route,
    compute_farming_route,
    get_advisor_opportunities,
    get_aggregate_shopping_list,
    get_all_nodes,
    get_farming_goals,
    get_goal_plan_with_progress,
    get_item_plan,
    get_pinned_goals,
    get_pinned_route,
    move_farming_goal,
    pin_route,
    remove_farming_goal,
    search_goal_items,
    set_checklist,
    set_goal_progress,
    update_farming_goal,
)
from services.request_ids import (
    RequestIdError,
    int64,
    item_ids_in,
    json_list,
    json_object,
    json_text,
    require_item,
)

farming_bp = Blueprint('farming', __name__, url_prefix='/farming')

_SESSION_NODE_KEY = 'advisor_node'


@farming_bp.route('/')
@login_required
def index():
    uid = current_user.id
    goals = get_farming_goals(uid)
    nodes_list = get_all_nodes()
    active_node = session.get(_SESSION_NODE_KEY)
    advisor_data = None
    if active_node:
        advisor_data = get_advisor_opportunities(uid, active_node)
    return render_template(
        'farming.html',
        goals=goals,
        nodes_list=nodes_list,
        active_node=active_node,
        advisor_data=advisor_data,
    )


# ── Goal management endpoints ────────────────────────────────────────────────

@farming_bp.route('/goals/search')
@login_required
def goals_search():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({'ok': True, 'items': []})
    items = search_goal_items(q, limit=20)
    return jsonify({'ok': True, 'items': items})


@farming_bp.route('/goals/add', methods=['POST'])
@login_required
def goals_add():
    try:
        data = json_object(request.get_json(force=True, silent=True))
        item_id = require_item(data.get('item_id'), strings=True).id
    except RequestIdError as err:
        return jsonify({'ok': False, 'error': err.message}), err.status
    goal = add_farming_goal(current_user.id, item_id)
    if not goal:
        return jsonify({'ok': False, 'error': 'Item not found'}), 404
    return jsonify({'ok': True, 'goal': goal, 'already_exists': goal.get('already_exists', False)})


@farming_bp.route('/goals/<id64:goal_id>/remove', methods=['POST'])
@login_required
def goals_remove(goal_id):
    remove_farming_goal(current_user.id, goal_id)
    return jsonify({'ok': True})


@farming_bp.route('/goals/<id64:goal_id>/update', methods=['POST'])
@login_required
def goals_update(goal_id):
    try:
        data = json_object(request.get_json(force=True, silent=True))
    except RequestIdError as err:
        return jsonify({'ok': False, 'error': err.message}), err.status
    kwargs = {}
    if 'pinned' in data:
        kwargs['pinned'] = int(bool(data['pinned']))
    if 'display_mode' in data and data['display_mode'] in ('tree', 'list'):
        kwargs['display_mode'] = data['display_mode']
    if 'sort_order' in data:
        try:
            kwargs['sort_order'] = int64(data['sort_order'])
        except (TypeError, ValueError):
            pass  # not an integer, or outside the 64-bit range: ignored
    update_farming_goal(current_user.id, goal_id, **kwargs)
    return jsonify({'ok': True})


@farming_bp.route('/goals/<id64:goal_id>/move', methods=['POST'])
@login_required
def goals_move(goal_id):
    try:
        data = json_object(request.get_json(force=True, silent=True))
    except RequestIdError as err:
        return jsonify({'ok': False, 'error': err.message}), err.status
    direction = data.get('direction')
    if not isinstance(direction, str) or direction not in GOAL_MOVE_DIRECTIONS:
        return jsonify({'ok': False, 'error': 'direction must be up or down'}), 400
    if not move_farming_goal(current_user.id, goal_id, direction):
        return jsonify({'ok': False, 'error': 'Goal not found'}), 404
    return jsonify({'ok': True})


@farming_bp.route('/goals/<id64:goal_id>/progress', methods=['POST'])
@login_required
def goals_progress(goal_id):
    data = request.get_json(force=True)
    try:
        row_type       = json_text(data['row_type'], 'row_type')
        row_key        = json_text(data['row_key'], 'row_key')
        acquired       = bool(data.get('acquired', False))
        quantity_owned = max(0, int64(data.get('quantity_owned', 0)))
        comp_state     = data.get('comp_state')  # None | 'pending' | 'blueprint' | 'crafted'
        if comp_state not in (None, 'pending', 'blueprint', 'crafted'):
            comp_state = None
    except (KeyError, TypeError, ValueError, RequestIdError):
        return jsonify({'ok': False, 'error': 'Invalid payload'}), 400
    if row_type not in ('component', 'ingredient'):
        return jsonify({'ok': False, 'error': 'row_type must be component or ingredient'}), 400
    written = set_goal_progress(current_user.id, goal_id, row_type, row_key, comp_state=comp_state,
                                acquired=acquired, quantity_owned=quantity_owned)
    if not written:
        return jsonify({'ok': False, 'error': 'Goal not found'}), 404
    return jsonify({'ok': True})


@farming_bp.route('/goals/<id64:goal_id>/plan')
@login_required
def goals_plan(goal_id):
    # `?prices=cached` (exactly): the cached goal price, nothing requested (a re-read)
    fetch_price = request.args.get('prices') != 'cached'
    plan = get_goal_plan_with_progress(goal_id, current_user.id, fetch_price)
    if not plan:
        return jsonify({'ok': False, 'error': 'Goal not found'}), 404
    return jsonify({'ok': True, 'plan': plan})


@farming_bp.route('/aggregate')
@login_required
def aggregate():
    data = get_aggregate_shopping_list(current_user.id)
    return jsonify({'ok': True, **data})


# ── Live Session Advisor ─────────────────────────────────────────────────────

@farming_bp.route('/nodes')
@login_required
def nodes():
    return jsonify({'ok': True, 'nodes': get_all_nodes()})


@farming_bp.route('/advisor/set', methods=['POST'])
@login_required
def advisor_set():
    try:
        data = json_object(request.get_json(force=True, silent=True))
        node = json_text(data.get('node_location', ''), 'node_location').strip()
    except RequestIdError as err:
        return jsonify({'ok': False, 'error': err.message}), err.status
    if node:
        session[_SESSION_NODE_KEY] = node
    else:
        session.pop(_SESSION_NODE_KEY, None)
    return jsonify({'ok': True, 'node': node or None})


@farming_bp.route('/advisor/clear', methods=['POST'])
@login_required
def advisor_clear():
    session.pop(_SESSION_NODE_KEY, None)
    return jsonify({'ok': True})


@farming_bp.route('/advisor')
@login_required
def advisor():
    node = session.get(_SESSION_NODE_KEY)
    if not node:
        return jsonify({'ok': True, 'node': None, 'opportunities': [], 'harvest_summary': [], 'guaranteed_free': False})
    data = get_advisor_opportunities(current_user.id, node)
    return jsonify({'ok': True, **data})


# ── Legacy routes (kept for backward compatibility) ──────────────────────────

@farming_bp.route('/compute', methods=['POST'])
@login_required
def compute():
    data = request.get_json(force=True, silent=True)
    item_ids = item_ids_in(data.get('item_ids') if isinstance(data, dict) else None)
    if not item_ids:
        return jsonify({'ok': False, 'error': 'No items selected'}), 400
    route = compute_farming_route(current_user.id, item_ids)
    return jsonify({'ok': True, 'route': route})


@farming_bp.route('/pin', methods=['POST'])
@login_required
def pin():
    try:
        data = json_object(request.get_json(force=True, silent=True))
        route = json_list(data.get('route', []), 'route')
    except RequestIdError as err:
        return jsonify({'ok': False, 'error': err.message}), err.status
    pin_route(current_user.id, route)
    return jsonify({'ok': True})


@farming_bp.route('/unpin', methods=['POST'])
@login_required
def unpin():
    clear_pinned_route(current_user.id)
    return jsonify({'ok': True})


@farming_bp.route('/plan', methods=['POST'])
@login_required
def plan():
    data = request.get_json(force=True, silent=True)
    item_ids = item_ids_in(data.get('item_ids') if isinstance(data, dict) else None)
    if not item_ids:
        return jsonify({'ok': False, 'error': 'No items selected'}), 400
    plans = [get_item_plan(iid, current_user.id) for iid in item_ids]
    plans = [p for p in plans if p]
    return jsonify({'ok': True, 'plans': plans})


@farming_bp.route('/checklist', methods=['POST'])
@login_required
def checklist():
    try:
        data = json_object(request.get_json(force=True, silent=True))
        item_id = require_item(data.get('item_id'), strings=True).id
    except RequestIdError as err:
        return jsonify({'ok': False, 'error': err.message}), err.status
    try:
        component_name = json_text(data['component_name'], 'component_name')
        acquired = bool(data.get('acquired', False))
    except (KeyError, TypeError, ValueError, RequestIdError):
        return jsonify({'ok': False, 'error': 'Invalid payload'}), 400
    set_checklist(current_user.id, item_id, component_name, acquired)
    return jsonify({'ok': True})


def _int_or_none(val):
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None
