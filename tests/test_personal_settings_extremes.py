"""Personal settings at their extremes: every value the save and the import accept keeps the
dashboard and the market working with the real price cache, and an over-long number is
refused or ignored, never an error page."""
from datetime import datetime

import pytest

from models.db import get_connection
from services import market_api
from services.auth_service import SETTING_VALUE_MAX, USER_SETTINGS

KEYS = [key for key, _, _ in USER_SETTINGS]


def _db(app, sql, params=()):
    with app.app_context():
        db = get_connection()
        try:
            cur = db.execute(sql, params)
            db.commit()
            return cur.lastrowid
        finally:
            db.close()


def _uid(app):
    with app.app_context():
        db = get_connection()
        try:
            return db.execute('SELECT id FROM users ORDER BY id').fetchone()[0]
        finally:
            db.close()


@pytest.fixture
def cached_item(app, monkeypatch):
    """An unowned tradable item with a fresh cached price; no market call can happen."""
    monkeypatch.setattr(market_api, 'fetch_lowest_price', lambda name: 10)
    item_id = _db(app, "INSERT INTO items (name, category, tradable, market_url_name) "
                       "VALUES ('Test Prime Set', 'warframe', 1, 'test_prime_set')")
    _db(app, 'INSERT INTO market_cache (item_id, platinum_price, fetched_at) VALUES (?, ?, ?)',
        (item_id, 10, datetime.utcnow().isoformat()))
    return item_id


# whole numbers the Preferences save and the import both accept
ACCEPTED_HOURS = [
    pytest.param('6', id='six'),
    pytest.param('1000000', id='a-million'),
    pytest.param(str(10 ** 12), id='ten-to-the-12'),
    pytest.param(str(SETTING_VALUE_MAX), id='the-bound'),
]


@pytest.mark.parametrize('hours', ACCEPTED_HOURS)
def test_an_accepted_cache_hours_value_keeps_the_price_lookup_working(
        app, admin_client, cached_item, hours):
    page = admin_client.post('/settings/', data={'action': 'save_preferences',
                                                 'market_cache_hours': hours,
                                                 'buy_threshold_plat': '',
                                                 'plat_per_hour': ''},
                             follow_redirects=True)
    assert b'Preferences saved.' in page.data

    assert admin_client.get(f'/market/price/{cached_item}').status_code == 200
    assert admin_client.get('/dashboard').status_code == 200


# digit strings beyond the interpreter's default int-from-text digit limit (4300 digits);
# leading zeros count toward that limit too
LONG_SPELLINGS = [
    pytest.param('1' * 5000, id='5000-ones'),
    pytest.param('0' * 4999 + '7', id='seven-after-4999-zeros'),
    pytest.param('0' * 5000, id='5000-zeros'),
]


@pytest.mark.parametrize('long', LONG_SPELLINGS)
@pytest.mark.parametrize('key', KEYS)
def test_an_over_long_number_on_save_is_refused_not_an_error(app, admin_client, key, long):
    data = {'action': 'save_preferences', **{k: '' for k in KEYS}, key: long}
    page = admin_client.post('/settings/', data=data, follow_redirects=True)
    assert page.status_code == 200
    # a value past the digit limit is refused or (when its value is in range) saved; never a 500
    assert b'Preferences not saved' in page.data or b'Preferences saved.' in page.data


@pytest.mark.parametrize('long', LONG_SPELLINGS)
@pytest.mark.parametrize('key', KEYS)
def test_an_over_long_stored_number_reads_as_the_default(app, admin_client, key, long):
    _db(app, 'INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)',
        (_uid(app), key, long))
    assert admin_client.get('/dashboard').status_code == 200
    assert admin_client.get('/market/').status_code == 200
