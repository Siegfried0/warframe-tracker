from flask import Blueprint, current_app, jsonify, render_template, request
from flask_login import login_required

from models.db import get_db
from services import image_cache, sync_schedule
from services.auth_service import admin_required
from services.wiki_sync import run_sync

sync_bp = Blueprint('sync', __name__, url_prefix='/sync')


@sync_bp.route('/')
@login_required
@admin_required
def index():
    return render_template('sync.html', last_sync=sync_schedule.current_status())


@sync_bp.route('/run', methods=['POST'])
@login_required
@admin_required
def run():
    status, answer = sync_schedule.run_from_page(
        current_app._get_current_object(), request.get_json(silent=True), run_sync)
    return jsonify(answer), status


@sync_bp.route('/image-coverage')
@login_required
@admin_required
def image_coverage():
    return jsonify({'ok': True, **image_cache.coverage(get_db())})
