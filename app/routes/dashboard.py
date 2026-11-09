from flask import Blueprint, render_template, session
from flask_login import current_user, login_required

from models.progress import Progress
from services.farming_service import (
    enrich_with_market,
    get_advisor_opportunities,
    get_pinned_goals,
)
from services.auth_service import get_setting_int
from services.market_api import watchlist_prices
from services.resurgence_sync import resurgence_status

dashboard_bp = Blueprint('dashboard', __name__)

_SESSION_NODE_KEY = 'advisor_node'

# Rows in the dashboard's market watch panel; the market page lists them all.
MARKET_WATCH_ROWS = 10


@dashboard_bp.route('/dashboard')
@login_required
def index():
    uid = current_user.id
    summary    = Progress.get_mastery_summary(uid)
    immediate  = Progress.get_immediate_gains(uid)
    resurgence_state = resurgence_status()
    resurgence = Progress.get_resurgence_watched_and_available(
        uid, resurgence_current=resurgence_state['current'])

    # Pinned farming goals with progress for "Farm next" panel
    pinned_goals = get_pinned_goals(uid)

    # Active session node → top secondary opportunities for the panel
    active_node = session.get(_SESSION_NODE_KEY)
    advisor_opportunities = []
    if active_node:
        advisor_data = get_advisor_opportunities(uid, active_node)
        advisor_opportunities = advisor_data.get('opportunities', [])[:3]

    # Legacy unowned list (shown when no pinned goals)
    cache_hours   = get_setting_int(uid, 'market_cache_hours')
    buy_threshold = get_setting_int(uid, 'buy_threshold_plat')
    plat_per_hour = get_setting_int(uid, 'plat_per_hour')
    unowned = Progress.get_unowned_items(uid)[:10]
    farm_next = enrich_with_market(uid, unowned, cache_hours, buy_threshold, plat_per_hour)

    # Market watch panel: cached prices only, never a request
    market_watch = watchlist_prices(uid, cache_hours, limit=MARKET_WATCH_ROWS)

    return render_template(
        'dashboard.html',
        summary=summary,
        immediate=immediate,
        farm_next=farm_next,
        resurgence=resurgence,
        resurgence_notice=resurgence_state['notice'],
        pinned_goals=pinned_goals,
        active_node=active_node,
        advisor_opportunities=advisor_opportunities,
        market_watch=market_watch,
        market_cache_hours=cache_hours,
    )

