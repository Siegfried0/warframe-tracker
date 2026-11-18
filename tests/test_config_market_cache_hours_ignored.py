"""The configuration no longer reads a market cache variable: any value in the environment,
even one that is not a number, is ignored and the configuration still loads.

Reloading the module rebinds every name in it (the Config class, the secret key helpers) to
new objects, while the application module keeps the ones it imported. So each reload here
puts the module's original namespace back afterwards: a later test that patches
config.Config must patch the class the application actually reads."""
import importlib

import pytest

import config


def _reload_has_market_cache_hours(monkeypatch, value):
    """Reload the configuration with the variable set to value, report whether the reloaded
    Config carries the setting, and leave the module exactly as it was found."""
    saved = dict(vars(config))
    monkeypatch.setenv('MARKET_CACHE_HOURS', value)
    try:
        reloaded = importlib.reload(config)
        return hasattr(reloaded.Config, 'MARKET_CACHE_HOURS')
    finally:
        monkeypatch.delenv('MARKET_CACHE_HOURS')
        namespace = vars(config)
        namespace.clear()
        namespace.update(saved)


@pytest.mark.parametrize('value', ['6', '0', '-1', 'abc', '', ' 12 ', '1e3'])
def test_a_market_cache_hours_value_in_the_environment_is_ignored(monkeypatch, value):
    assert not _reload_has_market_cache_hours(monkeypatch, value)


def test_the_reload_leaves_the_config_module_as_it_found_it(monkeypatch):
    config_class = config.Config
    require_usable_secret_key = config.require_usable_secret_key
    _reload_has_market_cache_hours(monkeypatch, '6')
    assert config.Config is config_class
    assert config.require_usable_secret_key is require_usable_secret_key
