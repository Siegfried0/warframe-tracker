from flask import request
from flask_login import current_user, login_user

from models.user import User
from services.csrf import rotate_token


def _before_request():
    if request.endpoint == 'static':
        return

    principal_name = request.headers.get('X-MS-CLIENT-PRINCIPAL-NAME')
    principal_id = request.headers.get('X-MS-CLIENT-PRINCIPAL-ID')

    if not principal_name or not principal_id:
        return

    user = User.get_by_oidc_sub(principal_id)
    if user is None:
        role = 'admin' if User.count() == 0 else 'user'
        user = User.create(
            username=principal_name,
            auth_type='easyauth',
            oidc_sub=principal_id,
            role=role,
        )

    if user and user.is_active:
        # A new sign-in (no user yet, or another one) gets a new CSRF token; the same user
        # on every request keeps the session's token.
        if not current_user.is_authenticated or current_user.id != user.id:
            rotate_token()
        login_user(user)


def register_easyauth(app):
    app.before_request(_before_request)
