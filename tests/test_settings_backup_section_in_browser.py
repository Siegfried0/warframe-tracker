"""The Settings page's backup section in a browser: it fits the page's widths and shows the
fields of the chosen strategy.

A location of any length stays inside the card (it wraps anywhere), at every width from a
phone to a wide desktop. With the page's script, only the chosen strategy's fieldset is shown,
and choosing the other strategy (a click on its label) swaps them; the hidden one is hidden,
never disabled, so a save still sends all six fields. Without the script both fieldsets are
shown. The radio buttons' labels are visible (Materialize hides the input itself) and every
control takes keyboard focus.

The page is rendered by the test client on its temporary database, its stylesheets and scripts
pointed at the app's own static files on disk, and laid out by a headless Chrome or Edge (a
local subprocess loading a local file, every host name resolving to nothing). Skipped when no
such browser is installed.
"""
import json
import os
import re
import subprocess

import pytest

from test_collection_card_height_in_browser import BROWSER

LONG_PATH = '/' + 'x' * 300 + '/<b>&"backups'

PROBE = """<script>
window.addEventListener('load', function () {
  var card = document.getElementById('backup-card'), form = card.querySelector('form.backup-retention');
  var box = card.getBoundingClientRect();
  function shown() {
    return Array.prototype.filter.call(form.querySelectorAll('fieldset[data-strategy]'),
      function (f) { return f.offsetParent !== null; })
      .map(function (f) { return f.getAttribute('data-strategy'); });
  }
  function sends() {
    var names = [];
    new FormData(form).forEach(function (v, k) { names.push(k); });
    return names;
  }
  var out = {
    overflow: card.scrollWidth - card.clientWidth,
    pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    ddOutside: Array.prototype.filter.call(card.querySelectorAll('dd'), function (d) {
      var r = d.getBoundingClientRect(); return r.right > box.right + 0.5 || r.left < box.left - 0.5;
    }).length,
    radioLabels: Array.prototype.map.call(form.querySelectorAll('[type=radio] + span'),
      function (s) { return s.getBoundingClientRect().width > 0 && getComputedStyle(s).visibility; }),
    focusable: Array.prototype.filter.call(form.querySelectorAll('[type=radio], input[type=text]'),
      function (i) { return i.closest('fieldset').offsetParent !== null; })
      .map(function (i) { i.focus(); return document.activeElement === i; }),
    before: shown(), sentBefore: sends()
  };
  var other = form.querySelector('input[name=strategy]:not(:checked)');
  other.nextElementSibling.click();
  out.checkedAfter = form.querySelector('input[name=strategy]:checked').value;
  out.after = shown();
  out.sentAfter = sends();
  var pre = document.createElement('pre');
  pre.id = 'probe';
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>"""


def probe(app, html, tmp_path, width, script=True):
    static = os.path.abspath(os.path.join(app.root_path, 'static')).replace(os.sep, '/')
    if not script:
        html, n = re.subn(r'<script src="/static/js/settings_backup\.js"></script>', '', html)
        assert n == 1
    html = re.sub(r'(href|src)="/static/', lambda m: f'{m.group(1)}="file:///{static}/', html)
    html = html.replace('</head>', PROBE + '</head>', 1)
    target = tmp_path / 'settings.html'
    target.write_text(html, encoding='utf-8')
    done = subprocess.run(
        [BROWSER, '--headless=new', '--disable-gpu', '--no-first-run',
         '--host-resolver-rules=MAP * ~NOTFOUND', '--disable-background-networking',
         '--disable-component-update', '--disable-sync', '--no-default-browser-check',
         f'--user-data-dir={tmp_path / "profile"}', f'--window-size={width},1400',
         '--virtual-time-budget=3000', '--dump-dom', target.as_uri()],
        capture_output=True, text=True, encoding='utf-8', timeout=60)
    found = re.search(r'<pre id="probe">(.*?)</pre>', done.stdout, re.S)
    assert found, done.stderr[-500:]
    import html as html_module
    return json.loads(html_module.unescape(found.group(1)))


SIX = ['strategy', 'min_days', 'keep', 'gfs_daily', 'gfs_weekly', 'gfs_monthly']


@pytest.mark.skipif(BROWSER is None, reason='no headless Chrome or Edge installed')
@pytest.mark.parametrize('script', [True, False], ids=['script', 'no-script'])
@pytest.mark.parametrize('width', [1920, 1280, 1000, 800, 600, 400],
                         ids=['1920px', '1280px', '1000px', '800px', '600px', '400px'])
def test_the_backup_section_fits_and_shows_the_chosen_strategys_fields(
        app, admin_client, tmp_path, width, script):
    app.config['BACKUP_LOCAL_PATH'] = LONG_PATH
    page = admin_client.get('/settings/').get_data(as_text=True)
    out = probe(app, page, tmp_path, width, script)
    assert out['overflow'] <= 0 and out['ddOutside'] == 0, out
    assert out['pageOverflow'] <= 0, out
    assert out['radioLabels'] == ['visible', 'visible'], out
    # Every control shown takes focus: two radios and the shown strategy's numbers.
    assert out['focusable'] == [True] * (4 if script else 7), out
    assert out['checkedAfter'] == 'gfs', out
    sent = [name for name in out['sentBefore'] if name in SIX]
    assert sent == SIX and [n for n in out['sentAfter'] if n in SIX] == SIX, out
    if script:
        assert out['before'] == ['interval_count'] and out['after'] == ['gfs'], out
    else:
        assert out['before'] == out['after'] == ['interval_count', 'gfs'], out
