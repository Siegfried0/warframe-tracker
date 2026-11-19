"""The Settings page's runtime section in a browser: it fits the page's widths, every control
takes keyboard focus and has a visible label, and the form sends exactly the editable fields:
a setting pinned by its environment variable is shown as text and is not sent. The section has
no script of its own, so what is measured here is what a browser without script does.

The page is rendered by the test client on its temporary database, its stylesheets pointed at
the app's own static files on disk, and laid out by a headless Chrome or Edge (a local
subprocess loading a local file, every host name resolving to nothing). Skipped when no such
browser is installed.
"""
import html as html_module
import json
import os
import re
import subprocess

import pytest

import app as app_module
from conftest import TokenClient
from models.db import get_db
from services import auth_service
from test_collection_card_height_in_browser import BROWSER

NAMES = ('AUTO_SYNC_ON_STARTUP', 'SYNC_INTERVAL_HOURS', 'SESSION_LIFETIME_DAYS')

PROBE = """<script>
window.addEventListener('load', function () {
  var card = document.getElementById('runtime-card'), form = document.getElementById('runtime-form');
  var controls = form.querySelectorAll('input:not([type=hidden])');
  var names = [];
  new FormData(form).forEach(function (v, k) { names.push(k); });
  var out = {
    overflow: card.scrollWidth - card.clientWidth,
    pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    labels: Array.prototype.map.call(form.querySelectorAll('[type=checkbox] + span'),
      function (s) { return s.getBoundingClientRect().width > 0 && getComputedStyle(s).visibility; }),
    focusable: Array.prototype.map.call(controls, function (i) { i.focus(); return document.activeElement === i; }),
    described: Array.prototype.map.call(form.querySelectorAll('input[aria-describedby]'), function (i) {
      return !!document.getElementById(i.getAttribute('aria-describedby')); }),
    sent: names
  };
  var pre = document.createElement('pre');
  pre.id = 'probe';
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
});
</script>"""


@pytest.fixture
def page(tmp_path, monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler
    monkeypatch.setattr(BackgroundScheduler, 'start', lambda self, *a, **k: None)
    overrides = {'TESTING': True, 'DATABASE_PATH': str(tmp_path / 'b.db'),
                 'SECRET_KEY': 'test-only', **{n: None for n in NAMES},
                 'SYNC_INTERVAL_HOURS': '0'}
    app = app_module.create_app(overrides)
    if hasattr(app, 'scheduler'):
        del app.scheduler
    from models.user import User
    with app.app_context():
        User.create('root', password='pw-root-111', role='admin')
        db = get_db()
        auth_service.upsert_global_setting(db, 'session_lifetime_days', '12')
        db.commit()
    client = TokenClient(app, app.response_class, use_cookies=True)
    client.post('/auth/login', data={'username': 'root', 'password': 'pw-root-111'})
    client.get('/settings/')
    return app, client.get('/settings/').get_data(as_text=True)


def probe(app, html, tmp_path, width):
    static = os.path.abspath(os.path.join(app.root_path, 'static')).replace(os.sep, '/')
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
    return json.loads(html_module.unescape(found.group(1)))


@pytest.mark.skipif(BROWSER is None, reason='no headless Chrome or Edge installed')
@pytest.mark.parametrize('width', [1920, 1000, 600, 400], ids=['1920px', '1000px', '600px',
                                                               '400px'])
def test_the_runtime_section_fits_takes_focus_and_sends_only_editable_fields(page, tmp_path,
                                                                             width):
    app, html = page
    out = probe(app, html, tmp_path, width)
    assert out['overflow'] <= 0 and out['pageOverflow'] <= 0, out
    # the switch and the lifetime's "use the default" box, each with a visible label
    assert out['labels'] == ['visible', 'visible'], out
    # the switch, the lifetime and its box: the pinned interval has no control
    assert out['focusable'] == [True, True, True], out
    assert out['described'] and all(out['described']), out
    # unticked boxes send nothing; the pinned interval is not sent
    assert out['sent'] == ['csrf_token', 'action', 'session_lifetime_days'], out
