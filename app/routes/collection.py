from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from models.item import CATEGORIES, CATEGORY_LABELS, COLLECTION_CATEGORIES, Item
from models.progress import Progress
from services.auth_service import get_setting_int
from services.collection_service import ToggleError, toggle_progress
from services.market_api import page_prices
from services.relic_sources import item_drop_sources
from services.request_ids import (
    RequestIdError,
    json_object,
    json_text,
    page_number,
    require_item,
)
from services.resurgence_sync import resurgence_status

collection_bp = Blueprint('collection', __name__, url_prefix='/collection')

_FILTER_KEYS = ('owned', 'mastered', 'subsumed', 'source', 'vaulted',
                'resurgence', 'modtype', 'polarity', 'targetmet')

_DEFAULT_PER_PAGE_CARDS = 48
_DEFAULT_PER_PAGE_TABLE = 100
_VALID_PER_PAGE = {24, 48, 96, 100, 200}


def _build_page_url(cat: str, filters: dict, page: int, per_page: int, q: str) -> str:
    params: dict = {k: v for k, v in filters.items() if v and v != 'all'}
    params['page'] = page
    params['per_page'] = per_page
    if q:
        params['q'] = q
    return url_for('collection.category', cat=cat, **params)


@collection_bp.route('/')
@login_required
def index():
    return redirect(url_for('collection.category', cat='warframe'))


@collection_bp.route('/<cat>')
@login_required
def category(cat):
    if cat not in CATEGORIES:
        abort(404)
    if cat == 'resource':
        return redirect(url_for('farming.index'))

    q = request.args.get('q', '').strip()

    filters = {k: request.args.get(k, 'all') for k in _FILTER_KEYS}

    try:
        per_page = int(request.args.get('per_page', _DEFAULT_PER_PAGE_CARDS))
    except (TypeError, ValueError):
        per_page = _DEFAULT_PER_PAGE_CARDS
    if per_page not in _VALID_PER_PAGE:
        per_page = _DEFAULT_PER_PAGE_CARDS

    page = page_number(request.args.get('page', 1), per_page)

    resurgence = resurgence_status()
    rows, total_items = Progress.get_paged_for_user(
        current_user.id, cat,
        filters=filters,
        query=q,
        page=page,
        per_page=per_page,
        resurgence_current=resurgence['current'],
    )

    total_pages = max(1, (total_items + per_page - 1) // per_page)
    page = min(page, total_pages)

    has_prev = page > 1
    has_next = page < total_pages

    prev_url = _build_page_url(cat, filters, page - 1, per_page, q) if has_prev else None
    next_url = _build_page_url(cat, filters, page + 1, per_page, q) if has_next else None

    filter_options = Progress.get_filter_options(current_user.id, cat)
    prices = page_prices(rows, get_setting_int(current_user.id, 'market_cache_hours'))

    return render_template(
        'collection.html',
        rows=rows,
        prices=prices,
        current_cat=cat,
        category_label=CATEGORY_LABELS[cat],
        categories=COLLECTION_CATEGORIES,
        category_labels=CATEGORY_LABELS,
        query=q,
        filters=filters,
        filter_options=filter_options,
        page=page,
        per_page=per_page,
        total_items=total_items,
        total_pages=total_pages,
        has_prev=has_prev,
        has_next=has_next,
        prev_url=prev_url,
        next_url=next_url,
        resurgence_notice=resurgence['notice'],
    )


@collection_bp.route('/toggle', methods=['POST'])
@login_required
def toggle():
    """AJAX endpoint — one toggle of a progress flag (the rule: services/collection_service)."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'invalid request'}), 400
    field = data.get('field')
    try:
        writes = toggle_progress(current_user.id, data.get('item_id'), field)
    except ToggleError as err:
        return jsonify({'ok': False, 'error': err.message}), err.status
    return jsonify({'ok': True, 'new_value': writes[field], 'written': writes})


@collection_bp.route('/note', methods=['POST'])
@login_required
def save_note():
    try:
        data = json_object(request.get_json(force=True, silent=True))
        item = require_item(data.get('item_id'), strings=True)
        note = json_text(data.get('note', ''), 'note', nullable=True)
    except RequestIdError as err:
        return jsonify({'ok': False, 'error': err.message}), err.status
    Progress.upsert(current_user.id, item.id, note=note)
    return jsonify({'ok': True})


@collection_bp.route('/update-rank', methods=['POST'])
@login_required
def update_rank():
    """Update owned_rank for an arcane (0 to mod_max_rank)."""
    try:
        data = json_object(request.get_json(force=True, silent=True))
        item = require_item(data.get('item_id'), strings=True)
    except RequestIdError as err:
        return jsonify({'ok': False, 'error': err.message}), err.status
    owned_rank = data.get('owned_rank')

    if owned_rank is None:
        return jsonify({'ok': False, 'error': 'item_id and owned_rank required'}), 400

    try:
        owned_rank = int(owned_rank)
    except (TypeError, ValueError, OverflowError):
        return jsonify({'ok': False, 'error': 'owned_rank must be an integer'}), 400

    if item.category != 'arcane':
        return jsonify({'ok': False, 'error': 'item not found or not an arcane'}), 404

    max_rank = item.mod_max_rank or 0
    owned_rank = max(0, min(owned_rank, max_rank))
    Progress.upsert(current_user.id, item.id, owned_rank=owned_rank)
    return jsonify({'ok': True, 'owned_rank': owned_rank})


@collection_bp.route('/update-target', methods=['POST'])
@login_required
def update_target():
    """Update target_rank for an arcane (0 to mod_max_rank)."""
    try:
        data = json_object(request.get_json(force=True, silent=True))
        item = require_item(data.get('item_id'), strings=True)
    except RequestIdError as err:
        return jsonify({'ok': False, 'error': err.message}), err.status
    target_rank = data.get('target_rank')

    if target_rank is None:
        return jsonify({'ok': False, 'error': 'item_id and target_rank required'}), 400

    try:
        target_rank = int(target_rank)
    except (TypeError, ValueError, OverflowError):
        return jsonify({'ok': False, 'error': 'target_rank must be an integer'}), 400

    if item.category != 'arcane':
        return jsonify({'ok': False, 'error': 'item not found or not an arcane'}), 404

    max_rank = item.mod_max_rank or 0
    target_rank = max(0, min(target_rank, max_rank))
    Progress.upsert(current_user.id, item.id, target_rank=target_rank)
    return jsonify({'ok': True, 'target_rank': target_rank})


@collection_bp.route('/drops/<id64:item_id>')
@login_required
def drops(item_id):
    """Return an item's drop sources and the relics holding its parts (the Farm-it modal)."""
    item = Item.get_by_id(item_id)
    if not item:
        return jsonify({'ok': False, 'error': 'item not found'}), 404
    return jsonify({'ok': True, 'item_name': item.name, **item_drop_sources(item_id)})
