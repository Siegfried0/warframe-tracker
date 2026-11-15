"""Every setting the configuration class defines is used somewhere in the app.

A setting the app reads from the environment but never uses misleads an operator: setting it
has no effect. This test reads the upper-case attributes from the ``Config`` class itself (not
from a hand-kept list), so a setting added later and never used fails the same way.

"Used" means the attribute's name appears as a whole word in at least one ``.py`` or ``.html``
file under ``app/`` other than ``config.py`` (whose own definition does not count). A known
limit: a mention in a comment of another file counts as a use.
"""
import re
from pathlib import Path

import config

APP_DIR = Path(config.__file__).resolve().parent
CONFIG_FILE = APP_DIR / 'config.py'


def config_settings():
    return sorted(name for name in vars(config.Config) if name.isupper())


def app_sources():
    """The text of every .py and .html file under app/ except config.py."""
    texts = []
    for path in sorted(APP_DIR.rglob('*')):
        if path.suffix not in ('.py', '.html') or not path.is_file():
            continue
        if path.resolve() == CONFIG_FILE:
            continue
        texts.append(path.read_text(encoding='utf-8', errors='replace'))
    return texts


def unused(settings, texts):
    """The names in ``settings`` that no text names as a whole word."""
    return [name for name in settings
            if not any(re.search(r'\b' + re.escape(name) + r'\b', text) for text in texts)]


def test_the_config_class_defines_settings():
    settings = config_settings()
    assert 'SECRET_KEY' in settings and 'DATABASE_PATH' in settings


def test_every_config_setting_is_used_outside_the_config_module():
    missing = unused(config_settings(), app_sources())
    assert missing == [], 'settings defined in config.py and used nowhere in app/: ' + ', '.join(missing)


def test_the_config_module_itself_is_not_counted():
    assert CONFIG_FILE.is_file()
    assert all('class Config' not in text for text in app_sources())


def test_the_word_match_needs_the_whole_name():
    texts = ['x = cfg.SECRET_KEY_OLD', 'value = MY_SECRET_KEY', 'mention: SECRET_KEY.']
    assert unused(['SECRET_KEY'], texts[:2]) == ['SECRET_KEY']
    assert unused(['SECRET_KEY'], texts) == []
