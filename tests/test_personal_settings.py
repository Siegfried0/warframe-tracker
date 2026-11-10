"""Personal settings: a stored value is read as a whole number or its default, and the
Preferences save accepts only empty or a whole number in the range the import accepts."""
import pytest

import routes.dashboard
import routes.market
from models.db import get_connection
from services import import_service
from services.auth_service import SETTING_VALUE_MAX, USER_SETTINGS

KEYS = [key for key, _, _ in USER_SETTINGS]
LABELS = {key: label for key, label, _ in USER_SETTINGS}


def _uid(app):
    with app.app_context():
        db = get_connection()
        try:
            return db.execute('SELECT id FROM users ORDER BY id').fetchone()[0]
        finally:
            db.close()


def _store(app, uid, key, value):
    """Write a settings row as stored text, bypassing every check (an old row, a hand edit)."""
    with app.app_context():
        db = get_connection()
        try:
            db.execute('DELETE FROM settings WHERE user_id = ? AND key = ?', (uid, key))
            db.execute('INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)',
                       (uid, key, value))
            db.commit()
        finally:
            db.close()


def _stored(app, uid):
    with app.app_context():
        db = get_connection()
        try:
            rows = db.execute('SELECT key, value FROM settings WHERE user_id = ?',
                              (uid,)).fetchall()
        finally:
            db.close()
    return {r[0]: r[1] for r in rows}


def _market_item(app):
    with app.app_context():
        db = get_connection()
        try:
            cur = db.execute("INSERT INTO items (name, category, tradable, market_url_name) "
                             "VALUES ('Test Prime Set', 'warframe', 1, 'test_prime_set')")
            db.commit()
            return cur.lastrowid
        finally:
            db.close()


@pytest.fixture
def capture(monkeypatch):
    """Record the cache hours and thresholds the pages pass on; nothing reaches a market."""
    seen = {}

    def fake_watch(uid, cache_hours, limit=None):
        seen['dashboard_hours'] = cache_hours
        return []

    def fake_farm_next(uid, buy_threshold, plat_per_hour, limit, cache_hours):
        seen['dashboard_thresholds'] = (buy_threshold, plat_per_hour)
        seen['farm_next_hours'] = cache_hours
        return []

    def fake_price(item_id, url_name, cache_hours):
        seen['price'] = cache_hours
        return {'platinum': 10}

    monkeypatch.setattr(routes.dashboard, 'watchlist_prices', fake_watch)
    monkeypatch.setattr(routes.dashboard, 'farm_next_items', fake_farm_next)
    monkeypatch.setattr(routes.market, 'get_price', fake_price)
    return seen


# stored text (None = NULL) -> the whole number a page uses, or None when it falls back
READ_CASES = [
    (None, None),
    ('', None),
    ('   ', None),
    ('abc', None),
    ('2.5', None),
    ('-1', None),
    ('1e3', None),
    ('+5', None),
    ('٣', None),                           # a non-ASCII digit is not a whole number here
    (str(SETTING_VALUE_MAX + 1), None),
    ('0', 0),
    ('6', 6),
    (' 7 ', 7),
    ('007', 7),
    (str(SETTING_VALUE_MAX), SETTING_VALUE_MAX),
]


@pytest.mark.parametrize('stored, number', READ_CASES)
def test_cache_hours_read_as_a_whole_number_or_six_on_every_page(
        app, admin_client, capture, stored, number):
    uid = _uid(app)
    _store(app, uid, 'market_cache_hours', stored)
    expected = 6 if number is None else number

    assert admin_client.get('/dashboard').status_code == 200
    assert capture['dashboard_hours'] == expected
    assert capture['farm_next_hours'] == expected

    page = admin_client.get('/market/')
    assert page.status_code == 200
    assert f'Cached for {expected} hour(s)'.encode() in page.data

    item_id = _market_item(app)
    price = admin_client.get(f'/market/price/{item_id}')
    assert price.status_code == 200
    assert capture['price'] == expected


@pytest.mark.parametrize('stored, number', READ_CASES)
def test_thresholds_read_as_a_whole_number_or_unset_on_the_dashboard(
        app, admin_client, capture, stored, number):
    uid = _uid(app)
    _store(app, uid, 'buy_threshold_plat', stored)
    _store(app, uid, 'plat_per_hour', stored)

    assert admin_client.get('/dashboard').status_code == 200
    assert capture['dashboard_thresholds'] == (number, number)


def test_no_settings_rows_read_as_the_defaults(app, admin_client, capture):
    assert admin_client.get('/dashboard').status_code == 200
    assert (capture['dashboard_hours'], capture['farm_next_hours'],
            *capture['dashboard_thresholds']) == (6, 6, None, None)


def _save(client, **fields):
    data = {'action': 'save_preferences', **{k: '' for k in KEYS}, **fields}
    return client.post('/settings/', data=data, follow_redirects=True)


# submitted text -> stored text, or a refusal
SAVE_CASES = [
    ('', None),
    ('   ', None),
    ('0', '0'),
    ('7', '7'),
    (' 7 ', '7'),
    ('007', '7'),
    (str(SETTING_VALUE_MAX), str(SETTING_VALUE_MAX)),
    ('abc', 'refused'),
    ('2.5', 'refused'),
    ('-1', 'refused'),
    ('+5', 'refused'),
    ('1e3', 'refused'),
    ('٣', 'refused'),
    (str(SETTING_VALUE_MAX + 1), 'refused'),
]


@pytest.mark.parametrize('key', KEYS)
@pytest.mark.parametrize('submitted, outcome', SAVE_CASES)
def test_preferences_save_accepts_only_empty_or_a_whole_number_in_range(
        app, admin_client, capture, key, submitted, outcome):
    uid = _uid(app)
    before = {'market_cache_hours': '12', 'buy_threshold_plat': '30', 'plat_per_hour': '9'}
    for k, v in before.items():
        _store(app, uid, k, v)
    others = {k: '5' for k in KEYS if k != key}

    page = _save(admin_client, **{key: submitted}, **others)
    assert page.status_code == 200
    stored = _stored(app, uid)

    if outcome == 'refused':
        assert (f'Preferences not saved: {LABELS[key]} must be empty or a whole number '
                f'from 0 to {SETTING_VALUE_MAX}.').encode() in page.data
        assert stored == before                      # none of the three keys written
    else:
        assert b'Preferences saved.' in page.data
        assert stored == {key: outcome, **others}
        # and every page still works with what was saved
        assert admin_client.get('/dashboard').status_code == 200
        assert admin_client.get('/market/').status_code == 200


def test_a_refusal_names_every_invalid_field_and_writes_nothing(app, admin_client):
    uid = _uid(app)
    _store(app, uid, 'plat_per_hour', '9')
    page = _save(admin_client, market_cache_hours='abc', buy_threshold_plat='-1',
                 plat_per_hour='4')
    assert (f"Preferences not saved: {LABELS['market_cache_hours']}, "
            f"{LABELS['buy_threshold_plat']} must be empty").encode() in page.data
    assert _stored(app, uid) == {'plat_per_hour': '9'}


def test_the_save_bound_is_the_import_bound():
    assert SETTING_VALUE_MAX == import_service._INT64_MAX


def test_a_save_interrupted_after_the_first_field_writes_nothing(app, admin_client,
                                                                  monkeypatch):
    from services import auth_service
    uid = _uid(app)
    before = {'market_cache_hours': '12', 'buy_threshold_plat': '30', 'plat_per_hour': '9'}
    for k, v in before.items():
        _store(app, uid, k, v)
    real, calls = auth_service._upsert_setting, []

    def failing_after_first(db, user_id, key, value):
        calls.append(key)
        if len(calls) > 1:
            raise RuntimeError('interrupted mid-save')
        real(db, user_id, key, value)

    monkeypatch.setattr(auth_service, '_upsert_setting', failing_after_first)
    app.config['PROPAGATE_EXCEPTIONS'] = False
    page = _save(admin_client, market_cache_hours='1', buy_threshold_plat='2',
                 plat_per_hour='3')
    assert page.status_code == 500
    assert len(calls) == 2                       # the first field was written, then it broke
    assert _stored(app, uid) == before           # and the write was rolled back
