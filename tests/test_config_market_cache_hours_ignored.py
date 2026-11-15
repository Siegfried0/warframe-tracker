"""The configuration no longer reads a market cache variable: any value in the environment,
even one that is not a number, is ignored and the configuration still loads."""
import importlib

import pytest

import config


@pytest.mark.parametrize('value', ['6', '0', '-1', 'abc', '', ' 12 ', '1e3'])
def test_a_market_cache_hours_value_in_the_environment_is_ignored(monkeypatch, value):
    monkeypatch.setenv('MARKET_CACHE_HOURS', value)
    try:
        reloaded = importlib.reload(config)
        assert not hasattr(reloaded.Config, 'MARKET_CACHE_HOURS')
    finally:
        monkeypatch.delenv('MARKET_CACHE_HOURS')
        importlib.reload(config)
