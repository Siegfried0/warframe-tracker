from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template
from flask_login import login_required

from models.db import get_db
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
    db = get_db()

    # Per-category counts from the DB
    cat_rows = db.execute('''
        SELECT category,
               COUNT(*)                                                          AS total,
               SUM(CASE WHEN image_url IS NULL            THEN 1 ELSE 0 END)    AS missing,
               SUM(CASE WHEN image_url LIKE '/static/%'   THEN 1 ELSE 0 END)    AS cached,
               SUM(CASE WHEN image_url IS NOT NULL
                         AND image_url NOT LIKE '/static/%' THEN 1 ELSE 0 END)  AS remote
          FROM items
         WHERE category != 'resource'
         GROUP BY category
         ORDER BY category
    ''').fetchall()
    categories = [dict(r) for r in cat_rows]

    # Cross-check cached paths against actual files on disk
    images_dir = Path(current_app.root_path) / 'static' / 'images' / 'items'
    disk_files: set[str] = set()
    if images_dir.exists():
        disk_files = {f.stem for f in images_dir.iterdir() if f.is_file()}

    # Items whose image_url points at a local path but the file is gone
    broken_rows = db.execute(
        "SELECT id FROM items WHERE image_url LIKE '/static/%'"
    ).fetchall()
    broken = sum(
        1 for r in broken_rows
        if str(r['id']) not in disk_files
    )

    # Items in the DB that have no image at all (excluding resources)
    missing_rows = db.execute(
        "SELECT id, name, category FROM items WHERE image_url IS NULL AND category != 'resource' ORDER BY category, name"
    ).fetchall()

    # Files on disk with no matching DB item (orphans)
    db_ids = {str(r['id']) for r in db.execute('SELECT id FROM items').fetchall()}
    orphan_count = sum(1 for stem in disk_files if stem not in db_ids)

    return jsonify({
        'ok':         True,
        'categories': categories,
        'broken':     broken,
        'orphans':    orphan_count,
        'disk_count': len(disk_files),
        'missing_items': [dict(r) for r in missing_rows],
    })
