from flask import abort, current_app, request
from flask_login import current_user, login_user

from models.db import INTEGRITY_ERRORS, get_db
from models.user import User
from services.csrf import rotate_token

# An EasyAuth sign-in that would have to take another account's row: the page says so; the
# reason (which column) goes to the log only.
REFUSED_MESSAGE = ('This sign-in cannot be matched to an account of this app. Ask the '
                   'administrator.')


def _before_request():
    if request.endpoint == 'static':
        return

    principal_name = request.headers.get('X-MS-CLIENT-PRINCIPAL-NAME')
    principal_id = request.headers.get('X-MS-CLIENT-PRINCIPAL-ID')

    if not principal_name or not principal_id:
        return

    user = User.get_easyauth_account(principal_id)
    if user is None:
        user = _create_account(principal_name, principal_id)
        if user is None:
            current_app.logger.warning(
                'EasyAuth sign-in refused: another account holds the name or principal id')
            abort(403, description=REFUSED_MESSAGE)

    if user and user.is_active:
        # A new sign-in (no user yet, or another one) gets a new CSRF token; the same user
        # on every request keeps the session's token.
        if not current_user.is_authenticated or current_user.id != user.id:
            rotate_token()
        login_user(user)


def _create_account(principal_name, principal_id):
    """The new EasyAuth account of ``principal_id``, or None when it cannot be made without
    taking another row: a local or OIDC row holds the id in the unique column, or the insert
    fails on a unique column and no EasyAuth row of this id exists afterwards (another row
    has the name). When it does exist, a parallel first request made it and it is used.
    The first account on an empty database is the admin, decided inside the insert."""
    if User.oidc_sub_held(principal_id, other_than='easyauth'):
        return None
    try:
        User.create_easyauth_account(principal_name, principal_id)
    except INTEGRITY_ERRORS:
        get_db().rollback()
    return User.get_easyauth_account(principal_id)


def register_easyauth(app):
    app.before_request(_before_request)
