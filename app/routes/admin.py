from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from models.db import get_db
from models.user import User
from services.auth_service import admin_required

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


@admin_bp.route('/')
@login_required
@admin_required
def index():
    users = User.get_all()
    stats = {}
    db = get_db()
    for u in users:
        cnt = db.execute(
            'SELECT COUNT(*) FROM user_progress WHERE user_id = ?', (u.id,)
        ).fetchone()[0]
        stats[u.id] = cnt
    return render_template('admin.html', users=users, stats=stats)


@admin_bp.route('/user/create', methods=['POST'])
@login_required
@admin_required
def create_user():
    if current_app.config.get('EASYAUTH_ENABLED'):
        abort(403)
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    email    = request.form.get('email', '').strip() or None
    role     = request.form.get('role', 'user')

    if not username or not password:
        flash('Username and password are required.', 'error')
    elif User.get_by_username(username):
        flash('Username already exists.', 'error')
    elif role not in ('admin', 'user'):
        flash('Invalid role.', 'error')
    else:
        User.create(username=username, password=password, email=email, role=role)
        flash(f'User {username} created.', 'success')
    return redirect(url_for('admin.index'))


@admin_bp.route('/user/<id64:uid>/role', methods=['POST'])
@login_required
@admin_required
def set_role(uid):
    if uid == current_user.id:
        flash('You cannot change your own role.', 'error')
        return redirect(url_for('admin.index'))
    user = User.get_by_id(uid)
    if not user:
        abort(404)
    role = request.form.get('role', 'user')
    if role not in ('admin', 'user'):
        flash('Invalid role.', 'error')
    else:
        user.set_role(role)
        flash(f'{user.username} is now {role}.', 'success')
    return redirect(url_for('admin.index'))


@admin_bp.route('/user/<id64:uid>/toggle', methods=['POST'])
@login_required
@admin_required
def toggle_active(uid):
    if uid == current_user.id:
        flash('You cannot deactivate yourself.', 'error')
        return redirect(url_for('admin.index'))
    user = User.get_by_id(uid)
    if not user:
        abort(404)
    user.set_active(not user.is_active)
    state = 'activated' if user.is_active else 'deactivated'
    flash(f'{user.username} {state}.', 'success')
    return redirect(url_for('admin.index'))


@admin_bp.route('/user/<id64:uid>/reset-password', methods=['POST'])
@login_required
@admin_required
def reset_password(uid):
    if current_app.config.get('EASYAUTH_ENABLED'):
        return jsonify(error='Not available in EasyAuth mode.'), 403
    user = User.get_by_id(uid)
    if not user:
        return jsonify(error='User not found.'), 404
    if user.auth_type != 'local':
        return jsonify(error='Password reset is only available for local-auth accounts.'), 403
    data = request.get_json(silent=True) or {}
    password = data.get('password', '')
    if not password:
        return jsonify(error='Password is required.'), 400
    if len(password) < 8:
        return jsonify(error='Password must be at least 8 characters.'), 400
    user.set_password(password)
    return jsonify(ok=True)


@admin_bp.route('/user/<id64:uid>/delete', methods=['POST'])
@login_required
@admin_required
def delete_user(uid):
    if uid == current_user.id:
        flash('Delete your account via Settings.', 'error')
        return redirect(url_for('admin.index'))
    user = User.get_by_id(uid)
    if not user:
        abort(404)
    confirm = request.form.get('confirm')
    if confirm != 'DELETE':
        flash('Type DELETE to confirm.', 'error')
    else:
        name = user.username
        user.delete()
        flash(f'User {name} deleted.', 'success')
    return redirect(url_for('admin.index'))
