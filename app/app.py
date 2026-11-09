import mimetypes
import os
import secrets
from datetime import timedelta
from typing import NamedTuple, Optional

from flask import Flask, redirect, url_for
from flask_login import LoginManager

from config import (SECRET_KEY_HELP, Config, require_usable_secret_key, secret_key_is_usable,
                    SettingError, session_cookie_samesite)
from models.db import init_db
from models.user import User
from services import csrf, oidc
from services.request_ids import Id64Converter, RequestJSONProvider

login_manager = LoginManager()


def create_app(config_overrides: dict = None):
    flask_app = Flask(__name__, template_folder='templates', static_folder='static')
    # The icon sprite is SVG: served as image/svg+xml whatever the host's MIME table says
    # (Windows reads it from the registry, where .svg can be missing or mapped elsewhere).
    mimetypes.add_type('image/svg+xml', '.svg')
    flask_app.config.from_object(Config)
    if config_overrides:
        flask_app.config.update(config_overrides)
    # Sessions and form tokens are signed with this key: no start with a missing or published
    # one, and nothing (the database included) is opened before this check.
    require_usable_secret_key(flask_app.config.get('SECRET_KEY'))
    # A SameSite value the cookie cannot carry would fail every response: refused here.
    flask_app.config['SESSION_COOKIE_SAMESITE'] = session_cookie_samesite(
        flask_app.config.get('SESSION_COOKIE_SAMESITE'))

    # <id64:...> in a route: a row id in the URL, 404 outside the signed 64-bit range.
    flask_app.url_map.converters['id64'] = Id64Converter
    # A JSON body nested too deep to parse is malformed (400), never a 500.
    flask_app.json = RequestJSONProvider(flask_app)

    flask_app.permanent_session_lifetime = timedelta(
        days=flask_app.config['SESSION_LIFETIME_DAYS']
    )

    # Every unsafe request carries the session's CSRF token; checked before any other hook.
    csrf.init_app(flask_app)

    login_manager.init_app(flask_app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Please log in to access this page.'
    login_manager.login_message_category = 'info'

    @login_manager.user_loader
    def load_user(user_id):
        return User.get_by_id(int(user_id))

    with flask_app.app_context():
        init_db()
        _register_blueprints(flask_app)
        _start_scheduler(flask_app)

    # A partial OIDC configuration leaves OIDC off; the log names the missing settings.
    oidc.warn_if_partial(flask_app)

    @flask_app.context_processor
    def _inject_globals():
        return {'easyauth_enabled': flask_app.config.get('EASYAUTH_ENABLED', False),
                'oidc_enabled': oidc.is_enabled(flask_app.config)}

    if flask_app.config.get('EASYAUTH_ENABLED'):
        from services.easyauth import register_easyauth
        register_easyauth(flask_app)

    return flask_app


def _register_blueprints(flask_app):
    from routes.auth import auth_bp
    from routes.dashboard import dashboard_bp
    from routes.collection import collection_bp
    from routes.farming import farming_bp
    from routes.market import market_bp
    from routes.settings import settings_bp
    from routes.admin import admin_bp
    from routes.sync import sync_bp

    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(dashboard_bp)
    flask_app.register_blueprint(collection_bp)
    flask_app.register_blueprint(farming_bp)
    flask_app.register_blueprint(market_bp)
    flask_app.register_blueprint(settings_bp)
    flask_app.register_blueprint(admin_bp)
    flask_app.register_blueprint(sync_bp)

    @flask_app.route('/')
    def index():
        return redirect(url_for('dashboard.index'))


def _start_scheduler(flask_app):
    if not flask_app.config.get('AUTO_SYNC_ON_STARTUP') and \
            not flask_app.config.get('SYNC_INTERVAL_HOURS'):
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from services.wiki_sync import run_sync

        scheduler = BackgroundScheduler(daemon=True)

        if flask_app.config.get('AUTO_SYNC_ON_STARTUP'):
            scheduler.add_job(lambda: run_sync(flask_app), 'date')

        interval_h = flask_app.config.get('SYNC_INTERVAL_HOURS', 0)
        if interval_h:
            scheduler.add_job(lambda: run_sync(flask_app), 'interval', hours=interval_h)

        scheduler.start()
        flask_app.scheduler = scheduler
    except ImportError:
        flask_app.logger.warning('APScheduler not installed — scheduled sync disabled.')


DEVELOPMENT_KEYLESS_WARNING = (
    'SECRET_KEY is not set (or is blank or the published default): this development server '
    'uses a random key for this process and listens on 127.0.0.1 only; logins end when it '
    'stops. ' + SECRET_KEY_HELP
)


DEVELOPMENT_HOST_IGNORED_WARNING = (
    'FLASK_RUN_HOST is set but was ignored: without a usable SECRET_KEY the development server '
    'listens on 127.0.0.1 only.'
)

# The addresses that reach this machine only; debug mode never listens anywhere else.
LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '::1')

DEVELOPMENT_DEBUG_REFUSED = (
    'The development server does not start: FLASK_DEBUG turns on the interactive debugger, '
    'which must never be reachable from other machines, and FLASK_RUN_HOST names an address '
    'other than 127.0.0.1, localhost or ::1. Unset FLASK_DEBUG, or set FLASK_RUN_HOST to a '
    'loopback address.'
)


class DevelopmentRunRefused(RuntimeError):
    """The development entry would serve the debugger beyond loopback."""


class DevelopmentRun(NamedTuple):
    host: str
    key: Optional[str]  # the key to use for this process, or None to keep the configured one
    debug: bool
    warning: Optional[str]


def development_debug_requested(value) -> bool:
    """FLASK_DEBUG turns debug mode on only as `1` or `true` (any case, blanks ignored)."""
    return (value or '').strip().lower() in ('1', 'true')


def development_run_settings(configured_key, environ=None) -> DevelopmentRun:
    """How the development entries (`python app.py`, `python run.py`) run.

    Host: 127.0.0.1 unless FLASK_RUN_HOST names another address; without a usable
    SECRET_KEY it stays 127.0.0.1 (a random key for this process only, so the keyless start
    is reachable from this machine alone) and the warning says the setting was ignored.
    Debug (the debugger and the reloader): off unless FLASK_DEBUG asks for it, and never
    beyond loopback: that combination raises DevelopmentRunRefused. Port: always 5000.
    """
    environ = os.environ if environ is None else environ
    requested_host = (environ.get('FLASK_RUN_HOST') or '').strip()
    debug = development_debug_requested(environ.get('FLASK_DEBUG'))
    if secret_key_is_usable(configured_key):
        host, key, warning = requested_host or '127.0.0.1', None, None
    else:
        host, key, warning = '127.0.0.1', secrets.token_hex(32), DEVELOPMENT_KEYLESS_WARNING
        if requested_host and requested_host != host:
            warning += ' ' + DEVELOPMENT_HOST_IGNORED_WARNING
    if debug and host.lower() not in LOOPBACK_HOSTS:
        raise DevelopmentRunRefused(DEVELOPMENT_DEBUG_REFUSED)
    return DevelopmentRun(host, key, debug, warning)


def run_development_server():
    """The development entry: decide how to run, build the app and serve it with Flask.

    A refused combination exits with its message and a non-zero status before anything
    (the database included) is opened.
    """
    try:
        run = development_run_settings(Config.SECRET_KEY)
    except DevelopmentRunRefused as refused:
        raise SystemExit(str(refused)) from None
    try:
        flask_app = create_app({'SECRET_KEY': run.key} if run.key is not None else None)
    except SettingError as refused:
        raise SystemExit(str(refused)) from None
    if run.warning:
        flask_app.logger.warning(run.warning)
    flask_app.run(host=run.host, port=5000, debug=run.debug)


if __name__ == '__main__':
    run_development_server()
else:
    # Module-level instance for Gunicorn / Azure App Service (refused without a usable key):
    #   gunicorn --bind=0.0.0.0 --timeout 600 app:app
    app = create_app()
