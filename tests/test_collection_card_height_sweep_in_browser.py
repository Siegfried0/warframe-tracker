"""Every card head on a tab has one height whatever the price shows: every tab x grid width x price.

Extends the browser height check to the other card tabs, more window widths and the price
values the note lists (0p, a large price such as 12345p, a NULL price, fresh and old). A mod
card carries two label lines (type; polarity and rank) beside the price.
"""
import pytest

from test_collection_card_height_in_browser import BROWSER, head_heights
from test_collection_prices import STALE_AT, FRESH, add_labelled, ago, cache, page, HEAD_TABS

PRICES = {
    # name suffix: cache row or None (on market), or 'off' (off the market)
    'Off': 'off',
    'Missing': None,
    'Fresh': (120, 'fresh'),
    'Old': (120, 'old'),
    'Zero Old': (0, 'old'),
    'Null Old': (None, 'old'),
    'Big Fresh': (12345, 'fresh'),
    'Big Old': (12345, 'old'),
}


@pytest.mark.skipif(BROWSER is None, reason='no headless Chrome or Edge installed')
@pytest.mark.parametrize('width', [1920, 1600, 1536, 1440, 1366, 1280, 1200, 1152, 1100,
                                   1024, 993, 992, 900, 800, 700, 601])
@pytest.mark.parametrize('tab', ['warframe', 'primary', 'companion', 'mod', 'arcane', 'k-drive'])
def test_every_head_on_a_tab_has_one_height_for_every_price(app, admin_client, tmp_path, tab, width):
    source = HEAD_TABS[tab][0]
    with app.app_context():
        for suffix, row in PRICES.items():
            iid = add_labelled(f'Item {suffix}', tab, row != 'off', source)
            if row not in ('off', None):
                price, state = row
                cache(iid, price, ago(FRESH) if state == 'fresh' else STALE_AT.isoformat())
    heights = head_heights(app, page(admin_client, tab), tmp_path, width)
    heights = {n: float(h) for n, h in heights.items()}
    assert len(heights) == len(PRICES), heights
    assert len(set(heights.values())) == 1, heights
