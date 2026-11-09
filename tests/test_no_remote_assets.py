"""No template or stylesheet of the app loads anything from another server.

The app works offline (apart from sync and price fetches, which run on the server): every
stylesheet, script, image, icon and font a page loads is served by the app itself. So no
template and no stylesheet under app/static/ names a remote address where the browser
fetches it:

- an HTML attribute that loads: `href`, `src`, `srcset`, `xlink:href` or `poster` on any
  element but a link the user follows (`<a>`, `<area>`): `<link href>`, `<script src>`,
  `<img src>`, `<use href>`, `<source src>`, ...;
- CSS, in a stylesheet or in a template's `<style>` block or `style` attribute: `@import`
  and `url()`.

A remote address starts with a scheme that leaves the app (`http:` or `https:`, in any case)
or is protocol-relative (`//host`, or with backslashes, which browsers read the same way),
after optional white space. A plain `<a href>` to an outside page is allowed: following it
is the user's choice, not a load. Addresses a Jinja expression makes (`url_for`) are the
app's own.

The detector matches patterns over the raw text rather than parsing HTML: a template is
Jinja, not HTML, and its page scripts build markup in JavaScript strings an HTML parser
would skip.

Offline: the tests read files only.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / 'app' / 'templates'
STATIC = ROOT / 'app' / 'static'

# A remote address: a scheme that leaves the app, or protocol-relative, after white space.
_REMOTE = re.compile(r'\s*(?:https?:|[/\\]{2})', re.I)

# An element's tag with its attributes; a quoted value may hold '>'.
_TAG = re.compile(r'''<([a-zA-Z][\w:-]*)((?:[^>"']|"[^"]*"|'[^']*')*)>''')
_LOADING_ATTR = re.compile(
    r'''(?<![\w:.-])(href|src|srcset|xlink:href|poster)\s*=\s*("[^"]*"|'[^']*'|[^\s>"']+)''',
    re.I)
_FOLLOWED = {'a', 'area'}

# CSS: @import with a string or url(), and every url().
_CSS_IMPORT = re.compile(r'''@import\s+(?:url\(\s*)?(["']?)([^"')\s;]*)''', re.I)
_CSS_URL = re.compile(r'''url\(\s*(["']?)([^"')]*)''', re.I)


def _unquote(value):
    if value[:1] in '"\'' and value[-1:] == value[:1]:
        return value[1:-1]
    return value


def _is_remote(address):
    return bool(_REMOTE.match(address))


def remote_loads(text):
    """Every remote address in `text` (a template or a stylesheet) that a browser would
    load, as `kind: address` strings."""
    found = []
    for tag, attrs in _TAG.findall(text):
        if tag.lower() in _FOLLOWED:
            continue
        for name, value in _LOADING_ATTR.findall(attrs):
            value = _unquote(value)
            addresses = [part.split()[0] for part in value.split(',') if part.split()] \
                if name.lower() == 'srcset' else [value]
            found += [f'<{tag} {name}>: {a.strip()}' for a in addresses if _is_remote(a)]
    for _quote, address in _CSS_IMPORT.findall(text):
        if _is_remote(address):
            found.append(f'@import: {address.strip()}')
    for _quote, address in _CSS_URL.findall(text):
        if _is_remote(address):
            found.append(f'url(): {address.strip()}')
    return found


# Axis 1, input spellings: each way a remote load can be written, and each look-alike that
# is not one.
REMOTE = [
    ('<link rel="stylesheet" href="https://fonts.example.com/icon?family=X">', 'link'),
    ('<link href="https://cdn.example.com/a.css" rel="stylesheet">', 'href before rel'),
    ('<link rel="stylesheet" href="http://cdn.example.com/a.css">', 'http'),
    ('<link rel="stylesheet" href="HTTPS://CDN.EXAMPLE.COM/A.CSS">', 'upper case'),
    ('<LINK REL="stylesheet" HREF="https://cdn.example.com/a.css">', 'upper-case tag'),
    ('<link rel="stylesheet" href="//cdn.example.com/a.css">', 'protocol-relative'),
    ('<link rel="stylesheet" href="\\\\cdn.example.com/a.css">', 'backslashes'),
    ("<link rel='stylesheet' href='https://cdn.example.com/a.css'>", 'single quotes'),
    ('<link rel=stylesheet href=https://cdn.example.com/a.css>', 'no quotes'),
    ('<link rel="stylesheet" href=" https://cdn.example.com/a.css">', 'leading space'),
    ('<link rel="stylesheet" href = "https://cdn.example.com/a.css">', 'spaces around ='),
    ('<link\n  rel="stylesheet"\n  href="https://cdn.example.com/a.css"\n>', 'several lines'),
    ('<link title="a > b" href="https://cdn.example.com/a.css">', '> in a quoted value'),
    ('<script src="https://cdn.example.com/x.js"></script>', 'script'),
    ('<script defer\n        src="//cdn.example.com/x.js"></script>', 'script, two lines'),
    ('<img src="https://img.example.com/a.png" alt="">', 'img'),
    ('<img srcset="/static/a.png 1x, https://img.example.com/a2.png 2x">', 'srcset'),
    ('<svg><use href="https://icons.example.com/s.svg#i-x"></use></svg>', 'use href'),
    ('<svg><use xlink:href="//icons.example.com/s.svg#i-x"></use></svg>', 'use xlink:href'),
    ('<video><source src="https://media.example.com/a.mp4"></video>', 'source'),
    ('<video poster="https://media.example.com/a.png"></video>', 'poster'),
    ('<iframe src="https://example.com/embed"></iframe>', 'iframe'),
    ('<style>@import url("https://fonts.example.com/css");</style>', '@import url()'),
    ("@import 'https://fonts.example.com/css';", '@import string'),
    ('@IMPORT "//fonts.example.com/css";', '@import upper case'),
    ('.a { background: url(https://img.example.com/a.png); }', 'url() bare'),
    ('.a { background: url( "https://img.example.com/a.png" ); }', 'url() spaced, quoted'),
    (".a { src: URL('//fonts.example.com/a.woff2'); }", 'url() upper case'),
    ('<div style="background:url(https://img.example.com/a.png)"></div>', 'url() in a style'),
]

LOCAL = [
    ('<a href="https://warframe.market/items/x" target="_blank">market</a>', 'a href'),
    ('<a\n   href="https://wiki.example.com/x"\n   class="grey-text">wiki</a>', 'a href, lines'),
    ('<area href="https://example.com/x">', 'area href'),
    ('<link rel="stylesheet" href="{{ url_for(\'static\', filename=\'css/app.css\') }}">',
     'url_for'),
    ('<link rel="stylesheet" href="/static/css/app.css">', 'absolute path'),
    ('<script src="static/js/app.js"></script>', 'relative path'),
    ('<img src="{{ row.image_url }}" alt="{{ row.name }}">', 'jinja value'),
    ('<use href="/static/icons/icons.svg#i-star"></use>', 'own sprite'),
    ('<svg xmlns="http://www.w3.org/2000/svg"></svg>', 'xmlns'),
    ('<div data-src="https://example.com/a.png"></div>', 'data- attribute'),
    ('<form action="https://example.com/post"></form>', 'form action'),
    ('<p>See https://example.com/a.css for more.</p>', 'text'),
    ('/* see https://example.com */ .a { color: red; }', 'css comment'),
    ('.a { background: url(/static/img/a.png); }', 'local url()'),
    ('.a { background: url("data:image/png;base64,AAAA"); }', 'data url()'),
    ("@import 'own.css';", 'local @import'),
    ("html += '<img src=\"' + escText(comp.image_url) + '\">';", 'script-built img'),
]


@pytest.mark.parametrize('text', [t for t, _ in REMOTE], ids=[i for _, i in REMOTE])
def test_a_remote_load_is_found_in_every_spelling(text):
    assert remote_loads(text) != []


@pytest.mark.parametrize('text', [t for t, _ in LOCAL], ids=[i for _, i in LOCAL])
def test_a_link_or_a_local_address_is_not_a_remote_load(text):
    assert remote_loads(text) == []


def test_the_finding_names_the_address():
    assert remote_loads('<script\n src="https://cdn.example.com/x.js">') == \
        ['<script src>: https://cdn.example.com/x.js']


def _project_files():
    files = sorted(TEMPLATES.rglob('*.html')) + sorted(STATIC.rglob('*.css'))
    return [p.relative_to(ROOT).as_posix() for p in files]


def test_the_scan_covers_every_template_and_the_project_stylesheet():
    files = _project_files()
    assert 'app/templates/base.html' in files
    assert 'app/templates/collection.html' in files
    assert 'app/templates/farming.html' in files
    assert 'app/static/css/app.css' in files


@pytest.mark.parametrize('path', _project_files())
def test_no_template_or_stylesheet_loads_a_remote_asset(path):
    assert remote_loads((ROOT / path).read_text(encoding='utf-8')) == []
