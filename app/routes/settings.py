import json
from datetime import datetime, timezone

from flask import (Blueprint, Response, current_app, flash, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required
from werkzeug.exceptions import RequestEntityTooLarge

from models.progress import Progress
from services.auth_service import get_setting, set_setting
from services.backup_service import backup_now
from services.export_service import export_user_data
from services.import_service import (MAX_REQUEST_BYTES, UPLOAD_TOO_LARGE, ImportRefused,
                                     import_user_data)

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
    ts = datetime.now(timezone.utc).strftime('%Y%m%d')
    return Response(
        export_user_data(current_user.id),
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename=warframe_progress_{ts}.zip'},
    )


@settings_bp.route('/import', methods=['POST'])
@login_required
def import_data():
    request.max_content_length = MAX_REQUEST_BYTES
    try:
        uploaded = request.files.get('import_file')
    except RequestEntityTooLarge:
        flash(UPLOAD_TOO_LARGE, 'error')
        return redirect(url_for('settings.index'))
    if not uploaded:
        flash('No file uploaded.', 'error')
        return redirect(url_for('settings.index'))
    try:
        result = import_user_data(current_user.id, uploaded.stream)
    except ImportRefused as exc:
        flash(str(exc), 'error')
    else:
        flash(result.message, 'success')
    return redirect(url_for('settings.index'))
