"""The market cache reload helper leaves the application's view of the config module, the
module object in sys.modules, and the environment as they were."""
import os
import sys

import pytest

import config
import test_config_market_cache_hours_ignored as reload_test


@pytest.mark.parametrize('value', ['6', 'abc', ''])
def test_the_app_and_the_config_module_hold_the_same_config_class_after_the_reload(
        monkeypatch, value):
    import app as app_module
    module_object = sys.modules['config']
    reload_test._reload_has_market_cache_hours(monkeypatch, value)
    assert sys.modules['config'] is module_object
    assert app_module.Config is config.Config
    assert app_module.require_usable_secret_key is config.require_usable_secret_key


@pytest.mark.parametrize('value', ['6', 'abc'])
def test_patching_config_class_after_the_reload_reaches_the_app(monkeypatch, value):
    import app as app_module
    reload_test._reload_has_market_cache_hours(monkeypatch, value)
    monkeypatch.setattr(config.Config, 'PROOF_MARKER', 'seen', raising=False)
    assert getattr(app_module.Config, 'PROOF_MARKER', None) == 'seen'


def test_an_unset_variable_stays_unset_after_the_helper(monkeypatch):
    monkeypatch.delenv('MARKET_CACHE_HOURS', raising=False)
    reload_test._reload_has_market_cache_hours(monkeypatch, '6')
    assert 'MARKET_CACHE_HOURS' not in os.environ


def test_a_second_pass_still_reports_the_setting_absent(monkeypatch):
    assert not reload_test._reload_has_market_cache_hours(monkeypatch, '6')
    assert not reload_test._reload_has_market_cache_hours(monkeypatch, '1e3')
