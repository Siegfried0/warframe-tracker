from flask import Blueprint, current_app, jsonify, render_template
from flask_login import login_required

from models.db import get_db
from services import image_cache
from services.auth_service import admin_required
from services.wiki_sync import run_sync

sync_bp = Blueprint('sync', __name__, url_prefix='/sync')


@sync_bp.route('/')
@login_required
@admin_required
def index():
    return render_template('sync.html')


@sync_bp.route('/run', methods=['POST'])
@login_required
@admin_required
def run():
    result = run_sync(current_app._get_current_object())
    return jsonify(result)


@sync_bp.route('/image-coverage')
@login_required
@admin_required
def image_coverage():
    return jsonify({'ok': True, **image_cache.coverage(get_db())})
