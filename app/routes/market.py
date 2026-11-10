from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from models.item import Item
from services.auth_service import get_setting_int
from services.component_prices import get_component_price
from services.market_api import get_price, unwatch_item, watch_item, watchlist_prices
from services.request_ids import RequestIdError, json_object, parse_item_id, require_item

market_bp = Blueprint('market', __name__, url_prefix='/market')


@market_bp.route('/')
@login_required
def index():
    uid = current_user.id
    cache_hours = get_setting_int(uid, 'market_cache_hours')
    watched = watchlist_prices(uid, cache_hours)
    return render_template('market.html', watched=watched,
                           prefs={'market_cache_hours': cache_hours})


@market_bp.route('/price/<id64:item_id>')
@login_required
def price(item_id):
    item = Item.get_by_id(item_id)
    if not item or not item.market_url_name:
        return jsonify({'ok': False, 'error': 'No market listing for this item'}), 404

    cache_hours = get_setting_int(current_user.id, 'market_cache_hours')
    return _price_answer(get_price(item_id, item.market_url_name, cache_hours))


# The longest component name a request may carry: the column holds 500 characters on MSSQL.
_COMPONENT_NAME_MAX = 500


@market_bp.route('/component-price/<id64:item_id>')
@login_required
def component_price(item_id):
    """One component's price (the item's id in the path, the component's name in the query
    value `component`, so any character of a name passes): answered as `price` answers."""
    name = request.args.get('component', '')
    if not name.strip() or len(name) > _COMPONENT_NAME_MAX:
        return jsonify({'ok': False, 'error': 'No market listing for this component'}), 404
    cache_hours = get_setting_int(current_user.id, 'market_cache_hours')
    data = get_component_price(item_id, name, cache_hours)
    if data is None:
        return jsonify({'ok': False, 'error': 'No market listing for this component'}), 404
    return _price_answer(data)


def _price_answer(data):
    if data.get('failed'):
        # The market failed, not the request: 502, with the price and fetch time kept.
        return jsonify({'ok': False, 'error': data['error'], 'platinum': data['platinum'],
                        'fetched_at': data['fetched_at'],
                        'fetched_label': data['fetched_label']}), 502
    return jsonify({'ok': True, **data})


@market_bp.route('/watch', methods=['POST'])
@login_required
def watch():
    try:
        data = json_object(request.get_json(force=True, silent=True))
        item_id = require_item(data.get('item_id'), strings=True).id
    except RequestIdError as err:
        return jsonify({'ok': False, 'error': err.message}), err.status
    watch_item(current_user.id, item_id)
    return jsonify({'ok': True})


@market_bp.route('/unwatch', methods=['POST'])
@login_required
def unwatch():
    # Idempotent: a well-formed id needs no existing item (nothing to remove is fine).
    try:
        data = json_object(request.get_json(force=True, silent=True))
        item_id = parse_item_id(data.get('item_id'), strings=True)
    except RequestIdError as err:
        return jsonify({'ok': False, 'error': err.message}), err.status
    unwatch_item(current_user.id, item_id)
    return jsonify({'ok': True})
