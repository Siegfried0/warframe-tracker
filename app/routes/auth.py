from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request,
                   session, url_for)
from flask_login import current_user, login_required, login_user

from models.user import User
from services import oidc
from services.auth_service import safe_return_path, sign_out
from services.csrf import rotate_token

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')


def _first_run_needed() -> bool:
    return User.count() == 0


@auth_bp.route('/setup', methods=['GET', 'POST'])
def setup():
    if current_app.config.get('EASYAUTH_ENABLED'):
        return redirect('/.auth/login')
    if not _first_run_needed():
        return redirect(url_for('dashboard.index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm', '')
        email    = request.form.get('email', '').strip() or None

        if not username or not password:
            flash('Username and password are required.', 'error')
        elif password != confirm:
            flash('Passwords do not match.', 'error')
        elif len(password) < 8:
            flash('Password must be at least 8 characters.', 'error')
        else:
            user = User.create_first_admin(username, password, email)
            if user is None:
                # Another setup or first sign-in made the first account meanwhile.
                flash('The administrator account already exists. Sign in.', 'error')
                return redirect(url_for('auth.login'))
            rotate_token()
            login_user(user, remember=True)
            flash(f'Welcome, {user.username}! Admin account created.', 'success')
            return redirect(url_for('dashboard.index'))
    return render_template('auth/setup.html')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_app.config.get('EASYAUTH_ENABLED'):
        return redirect('/.auth/login')
    if _first_run_needed():
        return redirect(url_for('auth.setup'))
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))

        user = User.get_by_username(username)
        if user and user.is_active and user.check_password(password):
            rotate_token()
            login_user(user, remember=remember)
            user.update_last_login()
            return redirect(safe_return_path(request.args.get('next'))
                            or url_for('dashboard.index'))
        flash('Invalid username or password.', 'error')
    return render_template('auth/login.html', next=safe_return_path(request.args.get('next')))


@auth_bp.route('/oidc/login')
def oidc_login():
    if current_app.config.get('EASYAUTH_ENABLED'):
        return redirect('/.auth/login')
    if not oidc.is_enabled(current_app.config):
        abort(404)
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))
    return_path = safe_return_path(request.args.get('next')) or url_for('dashboard.index')
    try:
        target = oidc.start_sign_in(current_app.config, session, return_path)
    except oidc.SignInRefused as refused:
        current_app.logger.warning('OIDC sign-in refused: %s', refused.reason)
        flash(refused.message, 'error')
        return redirect(url_for('auth.login'))
    return redirect(target)


@auth_bp.route('/oidc/callback')
def oidc_callback():
    if current_app.config.get('EASYAUTH_ENABLED'):
        return redirect('/.auth/login')
    if not oidc.is_enabled(current_app.config):
        abort(404)
    try:
        user, return_path = oidc.finish_sign_in(current_app.config, session, request.args)
    except oidc.SignInRefused as refused:
        current_app.logger.warning('OIDC sign-in refused: %s', refused.reason)
        flash(refused.message, 'error')
        return redirect(url_for('auth.login'))
    rotate_token()
    login_user(user, remember=True)
    user.update_last_login()
    return redirect(safe_return_path(return_path) or url_for('dashboard.index'))


# POST only (with the CSRF token, like every unsafe request): a GET never signs anyone out.
@auth_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    sign_out(session)
    if current_app.config.get('EASYAUTH_ENABLED'):
        return redirect('/.auth/logout')
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_app.config.get('EASYAUTH_ENABLED'):
        return redirect('/.auth/login')
    if _first_run_needed():
        return redirect(url_for('auth.setup'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm', '')
        email    = request.form.get('email', '').strip() or None

        error = None
        if not username:
            error = 'Username is required.'
        elif not password:
            error = 'Password is required.'
        elif password != confirm:
            error = 'Passwords do not match.'
        elif len(password) < 8:
            error = 'Password must be at least 8 characters.'
        elif User.get_by_username(username):
            error = 'Username already taken.'

        if error:
            flash(error, 'error')
        else:
            user = User.create(username=username, password=password, email=email)
            rotate_token()
            login_user(user, remember=True)
            user.update_last_login()
            flash(f'Account created. Welcome, {user.username}!', 'success')
            return redirect(url_for('dashboard.index'))

    return render_template('auth/register.html')
