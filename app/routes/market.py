from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from models.db import get_db
from models.item import Item
from services.auth_service import get_setting
from services.market_api import get_price, market_url_for

market_bp = Blueprint('market', __name__, url_prefix='/market')


@market_bp.route('/')
@login_required
def index():
    uid = current_user.id
    db = get_db()
    rows = db.execute('''
        SELECT i.*, mc.platinum_price, mc.fetched_at
        FROM user_market_watchlist w
        JOIN items i ON i.id = w.item_id
        LEFT JOIN market_cache mc ON mc.item_id = i.id
        WHERE w.user_id = ?
        ORDER BY i.name
    ''', (uid,)).fetchall()
    watched = [dict(r) for r in rows]
    for item in watched:
        if item.get('market_url_name'):
            item['market_url'] = market_url_for(item['market_url_name'])
    cache_hours = get_setting(uid, 'market_cache_hours', 6)
    return render_template('market.html', watched=watched,
                           prefs={'market_cache_hours': cache_hours})


@market_bp.route('/price/<int:item_id>')
@login_required
def price(item_id):
    item = Item.get_by_id(item_id)
    if not item or not item.market_url_name:
        return jsonify({'ok': False, 'error': 'No market listing for this item'}), 404

    cache_hours = int(get_setting(current_user.id, 'market_cache_hours', 6))
    data = get_price(item_id, item.market_url_name, cache_hours)
    return jsonify({'ok': True, **data})


@market_bp.route('/watch', methods=['POST'])
@login_required
def watch():
    item_id = request.get_json(force=True).get('item_id')
    if not item_id:
        return jsonify({'ok': False, 'error': 'item_id required'}), 400
    db = get_db()
    db.execute(
        '''INSERT OR IGNORE INTO user_market_watchlist (user_id, item_id) VALUES (?, ?)''',
        (current_user.id, item_id),
    )
    db.commit()
    return jsonify({'ok': True})


@market_bp.route('/unwatch', methods=['POST'])
@login_required
def unwatch():
    item_id = request.get_json(force=True).get('item_id')
    if not item_id:
        return jsonify({'ok': False, 'error': 'item_id required'}), 400
    db = get_db()
    db.execute(
        'DELETE FROM user_market_watchlist WHERE user_id = ? AND item_id = ?',
        (current_user.id, item_id),
    )
    db.commit()
    return jsonify({'ok': True})
