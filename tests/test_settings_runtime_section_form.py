"""The runtime section's form, field by field: every spelling of a number, of the switch and of a
"use the default" box, a field sent twice, a pinned setting's field, a missing field. The form's
check decides what a save writes, removes or refuses; a refusal names the field and saves
nothing. Checked on the form check itself, and through the page for the spellings a browser
cannot type (bytes that are not UTF-8).
"""
import pytest
from werkzeug.datastructures import MultiDict

import app as app_module
from conftest import TokenClient
from models.db import get_db
from services import runtime_page
from services import runtime_settings as rs

NAMES = ('AUTO_SYNC_ON_STARTUP', 'SYNC_INTERVAL_HOURS', 'SESSION_LIFETIME_DAYS')
KEYS = (rs.AUTO_SYNC, rs.INTERVAL, rs.LIFETIME)


@pytest.fixture(autouse=True)
def no_scheduler_start(monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler
    monkeypatch.setattr(BackgroundScheduler, 'start', lambda self, *a, **k: None)


def make_app(tmp_path, **pins):
    overrides = {'TESTING': True, 'DATABASE_PATH': str(tmp_path / 'form.db'),
                 'SECRET_KEY': 'test-only', **{n: None for n in NAMES}}
    overrides.update(pins)
    flask_app = app_module.create_app(overrides)
    if hasattr(flask_app, 'scheduler'):
        del flask_app.scheduler
    return flask_app


@pytest.fixture
def rapp(tmp_path):
    return make_app(tmp_path)


def base(**fields):
    data = {'action': runtime_page.ACTION, rs.AUTO_SYNC: 'on', rs.INTERVAL: '24',
            rs.LIFETIME: '30'}
    data.update(fields)
    return [(k, v) for k, v in data.items() if v is not None]


def check(app, pairs):
    return runtime_page.check_runtime_form(MultiDict(pairs), app)


# ── Axis 1: a number's spellings ─────────────────────────────────────────────

def number_cases(low, high):
    return [
        (str(low), str(low)), (str(high), str(high)), (f' {high} ', str(high)),
        ('\t7\n', '7'), ('0007', '7'), (str(high + 1), None), (str(low - 1), None),
        ('', None), ('   ', None), ('-1', None), ('+7', None), ('7.0', None), ('7.5', None),
        ('1e3', None), ('1_000', None), ('0x10', None), ('١٢', None), ('７', None),
        ('99999999999999999999999', None), ('18446744073709551617', None), ('12345', None),
        ('seven', None), ('7 7', None), ('\ud800', None), ('7\x00', None), ('７ ', None),
        # Python's strip() takes every Unicode blank, as the reader's own check does
        (' 7', '7'), (' 7　', '7'),
    ]


@pytest.mark.parametrize('key,low,high', [(rs.INTERVAL, 0, 8760), (rs.LIFETIME, 1, 365)],
                         ids=['interval', 'lifetime'])
def test_a_number_is_stored_normalised_or_refused_naming_the_field(rapp, key, low, high):
    for raw, stored in number_cases(low, high):
        writes, deletes, problems = check(rapp, base(**{key: raw}))
        if stored is None:
            assert writes.get(key) is None and problems, raw
            assert problems == [f'{rs.SETTINGS[key].label} must be a whole number from {low} '
                                f'to {high} {rs.SETTINGS[key].unit}'], (raw, problems)
        else:
            assert problems == [] and writes[key] == stored and deletes == [], raw


@pytest.mark.parametrize('key', [rs.INTERVAL, rs.LIFETIME])
def test_a_missing_number_field_is_refused_naming_it(rapp, key):
    _, _, problems = check(rapp, base(**{key: None}))
    assert problems == [f'{rs.SETTINGS[key].label} was not sent']


@pytest.mark.parametrize('key', [rs.INTERVAL, rs.LIFETIME])
def test_a_missing_number_field_is_refused_even_with_its_default_box(rapp, key):
    _, _, problems = check(rapp, base(**{key: None, key + '_default': 'on'}))
    assert problems == [f'{rs.SETTINGS[key].label} was not sent']


# ── Axis 1: the switch's spellings ───────────────────────────────────────────

@pytest.mark.parametrize('raw,stored', [
    (None, 'false'), ('on', 'true'), ('ON', None), ('On', None), (' on', None), ('on ', None),
    ('true', None), ('TRUE', None), ('1', None), ('', None), ('off', None), ('yes', None),
    ('\ud800', None), ('on\x00', None),
], ids=lambda v: repr(v))
def test_the_switch_is_exactly_on_or_absent(rapp, raw, stored):
    writes, _, problems = check(rapp, base(**{rs.AUTO_SYNC: raw}))
    if stored is None:
        assert problems == ['Sync on startup must be ticked or not ticked'] and rs.AUTO_SYNC \
            not in writes
    else:
        assert problems == [] and writes[rs.AUTO_SYNC] == stored


# ── Axis 1: "use the default" ────────────────────────────────────────────────

@pytest.mark.parametrize('key,value', [(rs.INTERVAL, ''), (rs.INTERVAL, '  '),
                                       (rs.LIFETIME, ''), (rs.AUTO_SYNC, None)],
                         ids=['interval empty', 'interval blanks', 'lifetime empty',
                              'switch unticked'])
def test_use_the_default_with_no_value_removes_the_row(rapp, key, value):
    writes, deletes, problems = check(rapp, base(**{key: value, key + '_default': 'on'}))
    assert problems == [] and deletes == [key] and key not in writes
    assert set(writes) == set(KEYS) - {key}


@pytest.mark.parametrize('key,value', [(rs.INTERVAL, '24'), (rs.INTERVAL, ' 6 '),
                                       (rs.INTERVAL, 'x'), (rs.LIFETIME, '30'),
                                       (rs.AUTO_SYNC, 'on')])
def test_a_value_and_use_the_default_are_refused_together(rapp, key, value):
    writes, deletes, problems = check(rapp, base(**{key: value, key + '_default': 'on'}))
    assert problems == [f'{rs.SETTINGS[key].label}: a value and "use the default" were both '
                        f'sent'] and deletes == []


@pytest.mark.parametrize('raw', ['', 'ON', 'true', '1', 'off', ' on', '\ud800', rs.INTERVAL])
@pytest.mark.parametrize('key', KEYS)
def test_a_default_box_is_exactly_on(rapp, key, raw):
    pairs = base(**{key: '' if key != rs.AUTO_SYNC else None, key + '_default': raw})
    _, deletes, problems = check(rapp, pairs)
    assert problems == [f'"Use the default" of {rs.SETTINGS[key].label} must be ticked or not '
                        f'ticked'] and deletes == []


def test_an_unknown_default_box_is_ignored(rapp):
    writes, deletes, problems = check(rapp, base(nothing_default='on', use_default='on'))
    assert problems == [] and deletes == [] and set(writes) == set(KEYS)


# ── Axis 1: repeated keys ────────────────────────────────────────────────────

@pytest.mark.parametrize('name,label', [
    ('action', 'Action'), (rs.AUTO_SYNC, 'Sync on startup'),
    (rs.INTERVAL, 'Scheduled sync interval'), (rs.LIFETIME, 'Session lifetime'),
    (rs.INTERVAL + '_default', '"Use the default" of Scheduled sync interval'),
    (rs.AUTO_SYNC + '_default', '"Use the default" of Sync on startup'),
])
@pytest.mark.parametrize('second', ['same', 'different'])
def test_a_key_sent_twice_is_refused_naming_it(rapp, name, label, second):
    pairs = base(**({rs.INTERVAL: ''} if name == rs.INTERVAL + '_default' else {}))
    if name == rs.AUTO_SYNC + '_default':
        pairs = base(**{rs.AUTO_SYNC: None})
    first = dict(pairs).get(name, 'on')
    pairs = [p for p in pairs if p[0] != name] + [(name, first),
                                                  (name, first if second == 'same' else '7')]
    writes, deletes, problems = check(rapp, pairs)
    assert f'{label} was sent more than once' in problems


# ── Pins ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('extra', [
    {rs.INTERVAL: '24'}, {rs.INTERVAL: ''}, {rs.INTERVAL + '_default': 'on'},
    {rs.INTERVAL: '', rs.INTERVAL + '_default': 'on'},
], ids=['value', 'empty value', 'default box', 'both'])
def test_a_pinned_field_or_its_default_box_is_refused_as_a_whole(tmp_path, extra):
    app = make_app(tmp_path, SYNC_INTERVAL_HOURS='0')
    pairs = base(**{rs.INTERVAL: None}) + list(extra.items())
    writes, deletes, problems = check(app, pairs)
    assert problems == ['Scheduled sync interval is set by the environment variable '
                        'SYNC_INTERVAL_HOURS and cannot be saved here']


def test_a_form_without_the_pinned_field_saves_the_others(tmp_path):
    app = make_app(tmp_path, SYNC_INTERVAL_HOURS='0')
    writes, deletes, problems = check(app, base(**{rs.INTERVAL: None}))
    assert problems == [] and set(writes) == {rs.AUTO_SYNC, rs.LIFETIME}


def test_a_pinned_switch_sent_unticked_is_untouched(tmp_path):
    app = make_app(tmp_path, AUTO_SYNC_ON_STARTUP='true')
    writes, _, problems = check(app, base(**{rs.AUTO_SYNC: None}))
    assert problems == [] and rs.AUTO_SYNC not in writes


def test_everything_pinned_saves_nothing(tmp_path):
    app = make_app(tmp_path, AUTO_SYNC_ON_STARTUP='true', SYNC_INTERVAL_HOURS='0',
                   SESSION_LIFETIME_DAYS='30')
    writes, deletes, problems = check(app, [('action', runtime_page.ACTION)])
    assert writes == {} and deletes == [] and len(problems) == 1


# ── Through the page: bytes a browser cannot type ────────────────────────────

def test_bytes_that_are_not_utf8_are_refused_and_nothing_is_written(rapp):
    from models.user import User
    with rapp.app_context():
        User.create('root', password='pw-root-111', role='admin')
    client = TokenClient(rapp, rapp.response_class, use_cookies=True)
    client.post('/auth/login', data={'username': 'root', 'password': 'pw-root-111'})
    body = (f'action={runtime_page.ACTION}&{rs.AUTO_SYNC}=on&{rs.INTERVAL}=%ED%A0%80'
            f'&{rs.LIFETIME}=%FF%FE')
    response = client.post('/settings/', data=body,
                           content_type='application/x-www-form-urlencoded')
    assert response.status_code == 302
    with rapp.app_context():
        found = get_db().execute('SELECT key FROM settings WHERE user_id IS NULL AND key IN '
                                 '(?, ?, ?)', KEYS).fetchall()
    assert found == []
