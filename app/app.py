from datetime import timedelta

from flask import Flask, redirect, url_for
from flask_login import LoginManager

from config import Config
from models.db import init_db
from models.user import User
from services.request_ids import Id64Converter

login_manager = LoginManager()


def create_app(config_overrides: dict = None):
    flask_app = Flask(__name__, template_folder='templates', static_folder='static')
    flask_app.config.from_object(Config)
    if config_overrides:
        flask_app.config.update(config_overrides)

    # <id64:...> in a route: a row id in the URL, 404 outside the signed 64-bit range.
    flask_app.url_map.converters['id64'] = Id64Converter

    flask_app.permanent_session_lifetime = timedelta(
        days=flask_app.config['SESSION_LIFETIME_DAYS']
    )

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


# Module-level instance for Gunicorn / Azure App Service:
#   gunicorn --bind=0.0.0.0 --timeout 600 app:app
app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
