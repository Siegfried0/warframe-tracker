from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from models.db import get_db
from models.item import CATEGORIES, CATEGORY_LABELS, COLLECTION_CATEGORIES, Item
from models.progress import Progress

collection_bp = Blueprint('collection', __name__, url_prefix='/collection')

_TOGGLE_FIELDS = {'in_possession', 'mastered', 'subsumed', 'resurgence_watch'}

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

    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1

    rows, total_items = Progress.get_paged_for_user(
        current_user.id, cat,
        filters=filters,
        query=q,
        page=page,
        per_page=per_page,
    )

    total_pages = max(1, (total_items + per_page - 1) // per_page)
    page = min(page, total_pages)

    has_prev = page > 1
    has_next = page < total_pages

    prev_url = _build_page_url(cat, filters, page - 1, per_page, q) if has_prev else None
    next_url = _build_page_url(cat, filters, page + 1, per_page, q) if has_next else None

    filter_options = Progress.get_filter_options(current_user.id, cat)

    return render_template(
        'collection.html',
        rows=rows,
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
    )


@collection_bp.route('/toggle', methods=['POST'])
@login_required
def toggle():
    """AJAX endpoint — toggle a boolean field on a user_progress row."""
    data    = request.get_json(force=True)
    item_id = data.get('item_id')
    field   = data.get('field')

    if not item_id or field not in _TOGGLE_FIELDS:
        return jsonify({'ok': False, 'error': 'invalid request'}), 400

    item = Item.get_by_id(item_id)
    if not item:
        return jsonify({'ok': False, 'error': 'item not found'}), 404

    current = Progress.get_for_user(current_user.id, item_id)
    new_val = 0 if (current and current.get(field)) else 1

    # Business rules
    if field == 'subsumed' and new_val == 1:
        # Subsuming destroys the Warframe — set mastered but leave in_possession alone
        Progress.upsert(current_user.id, item_id, mastered=1, subsumed=1)
    elif field == 'mastered' and new_val == 1:
        already_subsumed = current and current.get('subsumed')
        if already_subsumed:
            Progress.upsert(current_user.id, item_id, mastered=1)
        else:
            Progress.upsert(current_user.id, item_id, in_possession=1, mastered=1)
    else:
        Progress.upsert(current_user.id, item_id, **{field: new_val})

    return jsonify({'ok': True, 'new_value': new_val})


@collection_bp.route('/note', methods=['POST'])
@login_required
def save_note():
    data    = request.get_json(force=True)
    item_id = data.get('item_id')
    note    = data.get('note', '')

    if not item_id:
        return jsonify({'ok': False, 'error': 'item_id required'}), 400

    Progress.upsert(current_user.id, item_id, note=note)
    return jsonify({'ok': True})


@collection_bp.route('/update-rank', methods=['POST'])
@login_required
def update_rank():
    """Update owned_rank for an arcane (0 to mod_max_rank)."""
    data       = request.get_json(force=True)
    item_id    = data.get('item_id')
    owned_rank = data.get('owned_rank')

    if item_id is None or owned_rank is None:
        return jsonify({'ok': False, 'error': 'item_id and owned_rank required'}), 400

    try:
        owned_rank = int(owned_rank)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'owned_rank must be an integer'}), 400

    item = Item.get_by_id(item_id)
    if not item or item.category != 'arcane':
        return jsonify({'ok': False, 'error': 'item not found or not an arcane'}), 404

    max_rank = item.mod_max_rank or 0
    owned_rank = max(0, min(owned_rank, max_rank))
    Progress.upsert(current_user.id, item_id, owned_rank=owned_rank)
    return jsonify({'ok': True, 'owned_rank': owned_rank})


@collection_bp.route('/update-target', methods=['POST'])
@login_required
def update_target():
    """Update target_rank for an arcane (0 to mod_max_rank)."""
    data        = request.get_json(force=True)
    item_id     = data.get('item_id')
    target_rank = data.get('target_rank')

    if item_id is None or target_rank is None:
        return jsonify({'ok': False, 'error': 'item_id and target_rank required'}), 400

    try:
        target_rank = int(target_rank)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'target_rank must be an integer'}), 400

    item = Item.get_by_id(item_id)
    if not item or item.category != 'arcane':
        return jsonify({'ok': False, 'error': 'item not found or not an arcane'}), 404

    max_rank = item.mod_max_rank or 0
    target_rank = max(0, min(target_rank, max_rank))
    Progress.upsert(current_user.id, item_id, target_rank=target_rank)
    return jsonify({'ok': True, 'target_rank': target_rank})


@collection_bp.route('/drops/<int:item_id>')
@login_required
def drops(item_id):
    """Return drop sources for an item (used by the Farm-it modal for mods/arcanes)."""
    item = Item.get_by_id(item_id)
    if not item:
        return jsonify({'ok': False, 'error': 'item not found'}), 404

    rows = get_db().execute(
        '''SELECT mission_type, location, drop_chance, avg_runs, notes
             FROM drop_sources
            WHERE item_id = ?
            ORDER BY drop_chance DESC
            LIMIT 20''',
        (item_id,),
    ).fetchall()

    return jsonify({
        'ok': True,
        'item_name': item.name,
        'drops': [dict(r) for r in rows],
    })
