"""The reload helper of the market cache test leaves every name of the config module as it
found it, and still does so when the reload itself fails half-way."""
import importlib

import pytest

import config
import test_config_market_cache_hours_ignored as reload_test


def test_every_name_of_the_config_module_is_the_same_object_after_the_reload(monkeypatch):
    before = dict(vars(config))
    reload_test._reload_has_market_cache_hours(monkeypatch, 'abc')
    after = vars(config)
    assert set(after) == set(before)
    changed = [name for name in before if after[name] is not before[name]]
    assert changed == []


def test_a_reload_that_fails_half_way_still_restores_the_module(monkeypatch):
    config_class = config.Config

    def broken_reload(module):
        module.Config = object()
        module.extra_name = 1
        raise RuntimeError('reload failed')

    monkeypatch.setattr(importlib, 'reload', broken_reload)
    with pytest.raises(RuntimeError):
        reload_test._reload_has_market_cache_hours(monkeypatch, '6')
    assert config.Config is config_class
    assert not hasattr(config, 'extra_name')
