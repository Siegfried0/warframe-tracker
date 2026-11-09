import secrets
from datetime import timedelta

from flask import Flask, redirect, url_for
from flask_login import LoginManager

from config import SECRET_KEY_HELP, Config, require_usable_secret_key, secret_key_is_usable
from models.db import init_db
from models.user import User
from services import csrf
from services.request_ids import Id64Converter, RequestJSONProvider

login_manager = LoginManager()


def create_app(config_overrides: dict = None):
    flask_app = Flask(__name__, template_folder='templates', static_folder='static')
    flask_app.config.from_object(Config)
    if config_overrides:
        flask_app.config.update(config_overrides)
    # Sessions and form tokens are signed with this key: no start with a missing or published
    # one, and nothing (the database included) is opened before this check.
    require_usable_secret_key(flask_app.config.get('SECRET_KEY'))

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

    @flask_app.context_processor
    def _inject_globals():
        return {'easyauth_enabled': flask_app.config.get('EASYAUTH_ENABLED', False)}

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


def development_run_settings(configured_key):
    """How `python app.py` runs: (host, secret key to use or None, warning or None).

    With a usable SECRET_KEY it runs as before (all interfaces, that key). Without one it is the single exemption from the startup check: a random key for this process only,
    and the loopback address only, so the keyless start is reachable from this machine alone.
    """
    if secret_key_is_usable(configured_key):
        return '0.0.0.0', None, None
    return '127.0.0.1', secrets.token_hex(32), DEVELOPMENT_KEYLESS_WARNING


def run_development_server():
    """The development entry (`python app.py`): build the app and serve it with Flask."""
    host, key, warning = development_run_settings(Config.SECRET_KEY)
    flask_app = create_app({'SECRET_KEY': key} if key is not None else None)
    if warning:
        flask_app.logger.warning(warning)
    flask_app.run(host=host, port=5000, debug=True)


if __name__ == '__main__':
    run_development_server()
else:
    # Module-level instance for Gunicorn / Azure App Service (refused without a usable key):
    #   gunicorn --bind=0.0.0.0 --timeout 600 app:app
    app = create_app()
