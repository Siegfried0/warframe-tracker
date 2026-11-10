"""The card grid's head has one height on every card of a tab, whatever the market price shows.

A card's head holds the name and one label line on the left and, for an item on the market,
its price on the right. Its height must not depend on whether the item is on the market or on
the price's state (fresh, old, missing), at every grid width of a desktop or tablet window
(three or four cards a row). Phones (two cards a row) may wrap a long "old" mark and are not
checked here. A refresh that failed adds its note on a line of its own under the price: it
never wraps the value line, so it leaves a missing price's head as it was and adds at most
that one line under an old mark, until the page reloads.

Heights exist only in a browser: the page is rendered by the test client on its temporary
database, its stylesheets and scripts are pointed at the app's own static files on disk, and a
headless Chrome or Edge (a local subprocess loading a local file; the page loads nothing
remote) lays it out and reports each head's height. Skipped when no such browser is installed.
"""
import os
import re
import shutil
import subprocess

import pytest

from test_collection_prices import STALE_AT, add_labelled, ago, cache, page, FRESH


def find_browser():
    for name in ('chrome', 'msedge', 'google-chrome', 'chromium', 'chromium-browser'):
        found = shutil.which(name)
        if found:
            return found
    for root in (os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)')):
        for rel in (('Google', 'Chrome', 'Application', 'chrome.exe'),
                    ('Microsoft', 'Edge', 'Application', 'msedge.exe')):
            if root and os.path.isfile(os.path.join(root, *rel)):
                return os.path.join(root, *rel)
    return None


BROWSER = find_browser()

PROBE = """<script>
document.addEventListener('DOMContentLoaded', function () {
  if (window.failRefresh) {
    // The refresh handler's own failure path, on every price that has a refresh button.
    window.wfPriceShowIn($('.wf-price').has('.wf-price-refresh'), null, false, 'timed out');
  }
  var rows = [];
  document.querySelectorAll('.coll-card').forEach(function (c) {
    var head = c.querySelector('.coll-card-head');
    if (!head || !c.offsetParent) return;
    rows.push(c.querySelector('.coll-card-name').textContent + '=' +
              head.getBoundingClientRect().height.toFixed(1));
  });
  var pre = document.createElement('pre');
  pre.id = 'heads';
  pre.textContent = rows.join('\\n');
  document.body.appendChild(pre);
});
</script>"""


def head_heights(app, html, tmp_path, width, fail_refresh=False):
    static = os.path.abspath(os.path.join(app.root_path, 'static')).replace(os.sep, '/')
    html = re.sub(r'(href|src)="/static/', lambda m: f'{m.group(1)}="file:///{static}/', html)
    flag = '<script>window.failRefresh = %s;</script>' % ('true' if fail_refresh else 'false')
    html = html.replace('</head>', flag + PROBE + '</head>', 1)
    target = tmp_path / 'cards.html'
    target.write_text(html, encoding='utf-8')
    done = subprocess.run(
        [BROWSER, '--headless=new', '--disable-gpu', '--no-first-run',
         # The page is a local file; the browser itself must not reach the network either
         # (its update, sync and safe-browsing calls): every host name resolves to nothing.
         '--host-resolver-rules=MAP * ~NOTFOUND', '--disable-background-networking',
         '--disable-component-update', '--disable-sync', '--no-default-browser-check',
         f'--user-data-dir={tmp_path / "profile"}', f'--window-size={width},1200',
         '--virtual-time-budget=3000', '--dump-dom', target.as_uri()],
        capture_output=True, text=True, encoding='utf-8', timeout=60)
    found = re.search(r'<pre id="heads">(.*?)</pre>', done.stdout, re.S)
    assert found, done.stderr[-500:]
    return dict(line.rsplit('=', 1) for line in found.group(1).strip().splitlines())


@pytest.mark.skipif(BROWSER is None, reason='no headless Chrome or Edge installed')
@pytest.mark.parametrize('fail_refresh', [False, True], ids=['as-shown', 'refresh-failed'])
@pytest.mark.parametrize('width', [1920, 1366, 1280, 1000, 800],
                         ids=['1920px', '1366px', '1280px', '1000px', '800px'])
def test_every_card_head_on_a_tab_has_one_height_whatever_the_price_shows(
        app, admin_client, tmp_path, width, fail_refresh):
    with app.app_context():
        add_labelled('Ash', 'warframe', False, 'standard')                # off the market
        fresh = add_labelled('Ash Prime', 'warframe', True, 'prime')
        cache(fresh, 120, ago(FRESH))
        old = add_labelled('Banshee Prime', 'warframe', True, 'prime')
        cache(old, 95, STALE_AT.isoformat())
        add_labelled('Chroma Prime', 'warframe', True, 'prime')           # no price cached
    heights = head_heights(app, page(admin_client, 'warframe'), tmp_path, width, fail_refresh)
    assert set(heights) == {'Ash', 'Ash Prime', 'Banshee Prime', 'Chroma Prime'}
    heights = {name: float(h) for name, h in heights.items()}
    if not fail_refresh:
        assert len(set(heights.values())) == 1, heights
        return
    # The off-market and fresh cards have no refresh button; the missing price's note fits.
    assert heights['Ash Prime'] == heights['Chroma Prime'] == heights['Ash'], heights
    # The old price's note: at most one .7rem line (about 12 px at a 15 px root).
    assert 0 <= heights['Banshee Prime'] - heights['Ash'] <= 13, heights
