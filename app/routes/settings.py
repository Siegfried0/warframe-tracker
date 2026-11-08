import csv
import io
import json
import zipfile
from datetime import datetime

from flask import (Blueprint, Response, current_app, flash, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required

from models.db import get_db
from models.item import CATEGORIES, Item
from models.progress import Progress
from models.user import User
from services.auth_service import get_setting, set_setting
from services.backup_service import backup_now

settings_bp = Blueprint('settings', __name__, url_prefix='/settings')

_USER_SETTINGS = [
    ('market_cache_hours', 'Market cache duration (hours)', '6'),
    ('buy_threshold_plat', 'Buy threshold (platinum)', ''),
    ('plat_per_hour',      'Platinum per hour (time-value)', ''),
]


@settings_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    uid = current_user.id
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'save_preferences':
            for key, _, _ in _USER_SETTINGS:
                val = request.form.get(key, '').strip() or None
                set_setting(uid, key, val)
            flash('Preferences saved.', 'success')

        elif action == 'change_password':
            current_pw = request.form.get('current_password', '')
            new_pw     = request.form.get('new_password', '')
            confirm_pw = request.form.get('confirm_password', '')
            if not current_user.check_password(current_pw):
                flash('Current password is incorrect.', 'error')
            elif new_pw != confirm_pw:
                flash('New passwords do not match.', 'error')
            elif len(new_pw) < 8:
                flash('Password must be at least 8 characters.', 'error')
            else:
                current_user.set_password(new_pw)
                flash('Password changed.', 'success')

        elif action == 'backup_now':
            result = backup_now(current_app._get_current_object(), force=True)
            flash(f'Backup result: {json.dumps(result)}', 'info')

        elif action == 'clear_progress':
            confirm = request.form.get('confirm_clear')
            if confirm == 'CLEAR':
                Progress.clear_all_for_user(uid)
                flash('All progress data cleared.', 'warning')
            else:
                flash('Type CLEAR to confirm.', 'error')

        elif action == 'delete_account':
            confirm = request.form.get('confirm_delete')
            if confirm == 'DELETE':
                current_user.delete()
                flash('Account deleted.', 'info')
                return redirect(url_for('auth.login'))
            else:
                flash('Type DELETE to confirm.', 'error')

        return redirect(url_for('settings.index'))

    prefs = {key: get_setting(uid, key, default) for key, _, default in _USER_SETTINGS}
    return render_template('settings.html', settings_meta=_USER_SETTINGS, prefs=prefs)


@settings_bp.route('/export')
@login_required
def export_data():
    uid = current_user.id
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for cat in CATEGORIES:
            rows = Progress.get_all_for_user(uid, category=cat)
            csv_buf = io.StringIO()
            writer = csv.DictWriter(csv_buf, fieldnames=[
                'name', 'category', 'source', 'in_possession',
                'mastered', 'subsumed', 'resurgence_watch', 'note',
            ])
            writer.writeheader()
            for r in rows:
                writer.writerow({k: r.get(k, '') for k in writer.fieldnames})
            zf.writestr(f'{cat}.csv', csv_buf.getvalue())
    buf.seek(0)
    ts = datetime.utcnow().strftime('%Y%m%d')
    return Response(
        buf.getvalue(),
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename=warframe_progress_{ts}.zip'},
    )


@settings_bp.route('/import', methods=['POST'])
@login_required
def import_data():
    uid = current_user.id
    uploaded = request.files.get('import_file')
    if not uploaded:
        flash('No file uploaded.', 'error')
        return redirect(url_for('settings.index'))

    try:
        with zipfile.ZipFile(io.BytesIO(uploaded.read())) as zf:
            for name in zf.namelist():
                cat = name.replace('.csv', '')
                if cat not in CATEGORIES:
                    continue
                content = zf.read(name).decode('utf-8')
                reader = csv.DictReader(io.StringIO(content))
                for row in reader:
                    item_name = row.get('name', '').strip()
                    if not item_name:
                        continue
                    item = next(
                        (i for i in Item.get_all(category=cat) if i.name == item_name),
                        None,
                    )
                    if not item:
                        Item.upsert(name=item_name, category=cat)
                        item = next(
                            (i for i in Item.get_all(category=cat) if i.name == item_name),
                            None,
                        )
                    if not item:
                        continue
                    Progress.upsert(
                        uid, item.id,
                        in_possession=int(row.get('in_possession') or 0),
                        mastered=int(row.get('mastered') or 0),
                        subsumed=int(row.get('subsumed') or 0),
                        resurgence_watch=int(row.get('resurgence_watch') or 0),
                        note=row.get('note') or None,
                    )
        flash('Import complete.', 'success')
    except Exception as exc:
        flash(f'Import failed: {exc}', 'error')

    return redirect(url_for('settings.index'))
